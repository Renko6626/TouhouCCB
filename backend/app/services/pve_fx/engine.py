"""Five-second FX scheduling; balances and episode state always come from DB."""
from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal

import numpy as np
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.base import User
from app.models.bot import BotProfile
from app.models.fx import FxPair, FxWallet
from app.services.market_locks import lock_user
from app.services.credit.ownership import OWNERSHIP
from app.services.pve_fx.accounts import read_fx_bot_values
from app.services.pve_fx.config import TICK_SECONDS, load_fx_config
from app.services.pve_fx.executor import execute_bot_order, save_observation_state
from app.services.pve_fx.registry import get_strategy, validate_strategy_params
from app.services.pve_fx.types import FxBotSnapshot, FxExecutionView, FxTickFrame

logger = logging.getLogger('thccb.pve_fx')


@dataclass
class PairRuntime:
    closes: deque = field(default_factory=lambda: deque(maxlen=600))
    inventory_ema: dict[int, tuple[float, int]] = field(default_factory=dict)
    z: float = 0.0
    last_tick_id: int | None = None
    scheduled_bots: int = 0
    last_tick: dict = field(default_factory=dict)
    shuffle_rng: object = field(default_factory=np.random.default_rng)


    def inventory_reference(self, half_life_seconds: int, log_price: float, tick_id: int) -> float:
        reference, previous_tick = self.inventory_ema.get(half_life_seconds, (log_price, -1))
        if previous_tick != tick_id:
            alpha = -math.expm1(-math.log(2) * TICK_SECONDS / half_life_seconds)
            reference += alpha * (log_price - reference)
            self.inventory_ema[half_life_seconds] = (reference, tick_id)
        return reference


class FxPveEngine:
    def __init__(self):
        self.pairs: dict[int, PairRuntime] = {}
        self._mark_rngs: dict[int, object] = {}
        self._arrival_rngs: dict[int, object] = {}
        self._logs: dict[int, deque] = {}
        self._ticking = False
        self.last_tick_result: dict = {}

    def get_log(self, profile_id: int) -> list[dict]:
        return list(reversed(self._logs.get(profile_id, ())))

    def snapshot(self, pair_id: int | None = None) -> dict:
        pairs = {pid: {'pair_id': pid, 'z': rt.z, 'scheduled_bots': rt.scheduled_bots,
                       'sample_count': len(rt.closes), 'last_tick': rt.last_tick}
                 for pid, rt in self.pairs.items() if pair_id is None or pid == pair_id}
        return {'tick_seconds': TICK_SECONDS, 'pairs': pairs, 'last_tick': self.last_tick_result}

    def _log(self, profile_id, event, msg, **extra):
        self._logs.setdefault(profile_id, deque(maxlen=60)).append({
            'ts': datetime.now(timezone.utc).isoformat(), 'event': event, 'msg': msg, **extra})

    async def tick(self) -> dict:
        if self._ticking:
            return {'skipped': 'reentry'}
        OWNERSHIP.require_writes()
        self._ticking = True
        try:
            now = datetime.now(timezone.utc)
            async with async_session_maker() as db:
                config = await load_fx_config(db)
                if not config.enabled:
                    for rt in self.pairs.values():
                        rt.scheduled_bots = 0
                    result = {'enabled': False}
                else:
                    result = {'enabled': True, 'pairs': {}}
            if config.enabled:
                enabled_pairs = {pid for pid, cfg in config.pair_configs.items() if cfg.enabled}
                for pid, rt in self.pairs.items():
                    if pid not in enabled_pairs:
                        rt.scheduled_bots = 0
                for pid in sorted(enabled_pairs):
                    try:
                        result['pairs'][pid] = await self._tick_pair(pid, config.pair_configs[pid], now)
                    except Exception:
                        logger.exception('fx_pve_pair_tick_failed pair=%s', pid)
                        result['pairs'][pid] = {'error': 'tick_failed'}
            self.last_tick_result = {'at': now.isoformat(), **result}
            return result
        finally:
            self._ticking = False

    async def _load_bot(self, profile_id, pair_id):
        """An event gets a new session, including fresh pool quotes and state."""
        async with async_session_maker() as db:
            row = (await db.execute(select(BotProfile, User, FxPair)
                .join(User, User.id == BotProfile.user_id)
                .join(FxPair, FxPair.id == BotProfile.fx_pair_id)
                .where(BotProfile.id == profile_id, BotProfile.market_kind == 'fx',
                       BotProfile.fx_pair_id == pair_id, BotProfile.status == 'active',
                       User.is_active.is_(True), User.is_bot.is_(True),
                       FxPair.status == 'trading', FxPair.archived.is_(False)))).first()
            if row is None:
                return None
            profile, user, pair = row
            wallet = (await db.execute(select(FxWallet).where(
                FxWallet.user_id == user.id, FxWallet.pair_id == pair_id))).scalars().first()
            bot = FxBotSnapshot(profile.id, user.id, pair_id, profile.template,
                validate_strategy_params(profile.template, profile.params), user.cash,
                wallet.foreign_amount if wallet else Decimal(0), dict(profile.strategy_state or {}))
            view = FxExecutionView(pair.gold_reserve, pair.foreign_reserve,
                                   pair.buy_fee_rate, pair.sell_fee_rate)
            return bot, view

    async def _active_profiles(self, pair_id, death_floor):
        async with async_session_maker() as db:
            rows = (await db.execute(select(BotProfile.id, BotProfile.user_id)
                .join(User, User.id == BotProfile.user_id)
                .where(BotProfile.market_kind == 'fx', BotProfile.fx_pair_id == pair_id,
                       BotProfile.status == 'active', User.is_active.is_(True),
                       User.is_bot.is_(True)))).all()
        alive, dead = [], 0
        for profile_id, user_id in rows:
            # Match account management's user -> profile lock order. Revalue after
            # locking so a concurrent funding/pause cannot be overwritten.
            async with async_session_maker() as db:
                user = await lock_user(db, user_id)
                profile = (await db.execute(select(BotProfile).where(BotProfile.id == profile_id)
                    .with_for_update().execution_options(populate_existing=True))).scalars().first()
                if profile is None or profile.status != 'active' or not user.is_active:
                    continue
                value = (await read_fx_bot_values(db, [profile_id])).get(profile_id)
                if value is not None and value.cash + value.liquidation_cash < death_floor:
                    profile.status = 'dead'
                    await db.commit()
                    dead += 1
                    self._log(profile_id, 'lifecycle', '资产低于死亡水位')
                else:
                    alive.append(profile_id)
        return alive, dead

    async def _tick_pair(self, pair_id, cfg, now):
        tick_id = int(now.timestamp()) // TICK_SECONDS
        async with async_session_maker() as db:
            pair = await db.get(FxPair, pair_id)
            if pair is None or pair.archived or pair.status != 'trading':
                if pair_id in self.pairs:
                    self.pairs[pair_id].scheduled_bots = 0
                return {'skipped': 'pair_unavailable'}
            price = pair.gold_reserve / pair.foreign_reserve
        rt = self.pairs.setdefault(pair_id, PairRuntime())
        if rt.last_tick_id == tick_id:
            return {'skipped': 'same_tick'}
        rt.last_tick_id = tick_id
        profiles, dead = await self._active_profiles(pair_id, cfg.death_floor_gold)
        loaded_bots = {}
        for profile_id in profiles:
            try:
                loaded = await self._load_bot(profile_id, pair_id)
                if loaded is not None:
                    loaded_bots[profile_id] = loaded[0]
            except Exception as exc:
                logger.exception('fx_pve_prepare_failed profile=%s', profile_id)
                self._log(profile_id, 'error', str(exc))
        # EMA inventory needs a scalar reference, not an inventory price history.
        # Retain only the active short windows / legacy rolling inventory windows.
        widths = []
        for bot in loaded_bots.values():
            if bot.template == 'episode':
                widths.append(max(bot.params['trend_window_seconds'], bot.params['mean_window_seconds']) // TICK_SECONDS)
            elif bot.template == 'inventory' and bot.params['inventory_ema_half_life_seconds'] is None:
                widths.append(bot.params['inventory_window_seconds'] // TICK_SECONDS)
        history_length = max(widths, default=1)
        if loaded_bots and rt.closes.maxlen != history_length:
            rt.closes = deque(rt.closes, maxlen=history_length)
        if not rt.closes:
            rt.closes.append(price)
        frame = FxTickFrame(tick_id, pair_id, now, tuple(rt.closes), price)
        prepared = {}
        # Prepare everyone even without arrivals; EMA and episode noise advance per tick.
        for profile_id, bot in loaded_bots.items():
            try:
                bot_frame = frame
                half_life = bot.params.get('inventory_ema_half_life_seconds')
                if bot.template == 'inventory' and half_life is not None:
                    reference = rt.inventory_reference(half_life, math.log(float(rt.closes[-1])), tick_id)
                    bot_frame = replace(frame, signals={'inventory_log_reference': reference})
                signal = get_strategy(bot.template).prepare_tick(bot, bot_frame,
                    self._mark_rngs.setdefault(profile_id, np.random.default_rng()))
                signal.values['max_pool_fraction'] = cfg.max_pool_fraction
                prepared[profile_id] = (bot.template, bot.params, signal)
            except Exception as exc:
                logger.exception('fx_pve_prepare_failed profile=%s', profile_id)
                self._log(profile_id, 'error', str(exc))
        rt.scheduled_bots = len(profiles)
        intensity = (len(profiles) / 20) * ((1 - cfg.activity_b) * cfg.mean_arrivals_per_20 + cfg.activity_b * rt.z ** 2)
        events = []
        for profile_id in profiles:
            rng = self._arrival_rngs.setdefault(profile_id, np.random.default_rng())
            events.extend([profile_id] * int(rng.poisson(intensity / len(profiles))))
        rt.shuffle_rng.shuffle(events)
        stats = {'bots': len(profiles), 'dead': dead, 'arrivals': len(events),
                 'trade': 0, 'skip': 0, 'error': 0, 'intensity': intensity}
        for profile_id in events:
            if profile_id not in prepared:
                stats['skip'] += 1
                continue
            try:
                loaded = await self._load_bot(profile_id, pair_id)
                if loaded is None:
                    stats['skip'] += 1
                    continue
                bot, view = loaded
                template, params, signal = prepared[profile_id]
                # A template/parameter edit applies next tick. Never combine a
                # prepared old signal with new strategy parameters.
                if bot.template != template or bot.params != params:
                    stats['skip'] += 1
                    continue
                decision = get_strategy(template).decide(bot, signal, view)
                if decision.intent is None:
                    await save_observation_state(profile_id, decision, frame)
                    stats['skip'] += 1
                    self._log(profile_id, 'skip', decision.reason)
                else:
                    result = await execute_bot_order(profile_id, decision, frame)
                    event = 'trade' if result.fill else 'skip'
                    stats[event] += 1
                    self._log(profile_id, event, result.rejection or decision.reason,
                              trade_id=result.fill.trade_id if result.fill else None)
            except Exception as exc:
                stats['error'] += 1
                logger.exception('fx_pve_event_failed profile=%s', profile_id)
                self._log(profile_id, 'error', str(exc))
        async with async_session_maker() as db:
            latest = await db.get(FxPair, pair_id)
            if latest is not None:
                close = latest.gold_reserve / latest.foreign_reserve
                change = math.log(float(close / rt.closes[-1]))
                a = math.exp(-TICK_SECONDS / cfg.tau_seconds)
                rt.z = a * rt.z + math.sqrt(1 - a * a) * change / cfg.psi
                rt.closes.append(close)
        rt.last_tick = {'tick_id': tick_id, **stats}
        return stats


ENGINE = FxPveEngine()


def get_fx_pve_engine() -> FxPveEngine:
    return ENGINE
