"""Deterministic arrivals through real strategies, spot AMM and persistence."""
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.base import SiteConfig, User
from app.models.bot import BotProfile
from app.models.fx import FxPair, FxTrade, FxTreasury
from app.schemas.pve_fx import FxPveConfig, FxPveGenerateRequest, FxPairBotConfig
from app.services import site_config
from app.services.pve_fx import accounts
from app.services.pve_fx.config import save_fx_config
from app.services.pve_fx.engine import FxPveEngine


class Draws:
    def __init__(self, *, arrivals=0, normal=2):
        self.arrivals, self.value, self.normal_calls = arrivals, normal, 0
        self.rates = []

    def poisson(self, rate):
        self.rates.append(rate)
        return self.arrivals

    def normal(self):
        self.normal_calls += 1
        return self.value


async def seed(items, cash='5000', foreign='1000'):
    async with async_session_maker() as db:
        pair = FxPair(currency_code='ENGINE', currency_name='Engine', status='trading',
                      gold_reserve=D('1000000'), foreign_reserve=D('200000'))
        operator = User(username='operator', is_superuser=True)
        db.add_all([pair, operator, SiteConfig(key='fx_enabled', value='true', value_type='bool')])
        await db.flush()
        db.add(FxTreasury(pair_id=pair.id))
        bots = await accounts.generate_fx_bots(db, FxPveGenerateRequest(pair_id=pair.id,
            items=items, initial_cash=cash, initial_foreign=foreign), operator.id)
        for bot in bots:
            (await db.get(BotProfile, bot.profile_id)).status = 'active'
        cfg = FxPairBotConfig(enabled=True)
        await save_fx_config(db, FxPveConfig(enabled=True, pair_configs={pair.id: cfg}), operator.id)
        await db.commit()
        site_config.clear_cache()
        return pair.id, cfg, bots


@pytest.mark.asyncio
async def test_no_arrival_episode_noise_and_wallet_only_death():
    pid, cfg, bots = await seed([{'template': 'episode', 'count': 1}], cash='0')
    bot = bots[0]
    engine = FxPveEngine()
    mark, arrival = Draws(), Draws()
    engine._mark_rngs[bot.profile_id] = mark
    engine._arrival_rngs[bot.profile_id] = arrival
    now = datetime.now(timezone.utc)
    result = await engine._tick_pair(pid, cfg, now)
    assert result['bots'] == 1 and result['dead'] == 0 and result['trade'] == 0
    assert mark.normal_calls == 1 and arrival.rates == [pytest.approx(.675 / 20)]
    assert await engine._tick_pair(pid, cfg, now) == {'skipped': 'same_tick'}
    assert mark.normal_calls == 1
    async with async_session_maker() as db:
        profile = await db.get(BotProfile, bot.profile_id)
        assert profile.status == 'active' and profile.strategy_state == {}
        # A genuinely empty account dies before it contributes arrival intensity.
        user = await db.get(User, bot.user_id)
        from app.models.fx import FxWallet
        wallet = (await db.execute(select(FxWallet).where(FxWallet.user_id == user.id))).scalars().one()
        wallet.foreign_amount = D('0')
        wallet.cost_basis = D('0')
        await db.commit()
    result = await engine._tick_pair(pid, cfg, now + timedelta(seconds=5))
    assert result['dead'] == 1 and result['bots'] == 0 and result['intensity'] == 0
    assert mark.normal_calls == 1


@pytest.mark.asyncio
async def test_fixed_prepared_inventory_fresh_views_and_same_tick_limits():
    pid, cfg, bots = await seed([{'template': 'random', 'count': 1},
                                {'template': 'inventory', 'count': 1},
                                {'template': 'episode', 'count': 1}])
    random_bot, inventory_bot, episode_bot = bots
    # Inventory must adjust a real excess over its immutable initial baseline.
    async with async_session_maker() as db:
        from app.models.fx import FxWallet
        wallet = (await db.execute(select(FxWallet).where(FxWallet.user_id == inventory_bot.user_id))).scalars().one()
        wallet.foreign_amount += D('100')
        await db.commit()
    engine = FxPveEngine()
    for bot in bots:
        engine._mark_rngs[bot.profile_id] = Draws(normal=2)
        engine._arrival_rngs[bot.profile_id] = Draws(arrivals=2)
    now = datetime.now(timezone.utc)
    # Make event ordering deterministic; production uses an independent shuffle stream.
    from app.services.pve_fx.engine import PairRuntime
    class Ordered:
        def shuffle(self, events):
            events.sort()
    engine.pairs[pid] = PairRuntime(shuffle_rng=Ordered())
    result = await engine._tick_pair(pid, cfg, now)
    assert result['trade'] == 4 and result['skip'] == 2 and result['error'] == 0
    async with async_session_maker() as db:
        trades = (await db.execute(select(FxTrade).order_by(FxTrade.id))).scalars().all()
        assert [t.user_id for t in trades] == [random_bot.user_id] * 2 + [inventory_bot.user_id, episode_bot.user_id]
        # The second event sees the first real pool change.
        assert trades[1].pre_gold_reserve == trades[0].post_gold_reserve
        assert trades[1].input_amount != trades[0].input_amount
        profile = await db.get(BotProfile, inventory_bot.profile_id)
        assert profile.strategy_state['last_action_tick'] == int(now.timestamp()) // 5
        # At the initial fixed price/history the target is zero: sell one eighth of the
        # 100 excess even though preceding buys changed the current pool price.
        assert trades[2].input_amount == D('12.5')
        episode = (await db.get(BotProfile, episode_bot.profile_id)).strategy_state['episode']
        assert episode['entry_tick'] == int(now.timestamp()) // 5
        pair = await db.get(FxPair, pid)
        close = pair.gold_reserve / pair.foreign_reserve
    rt = engine.pairs[pid]
    assert rt.z == pytest.approx(math.sqrt(1 - math.exp(-10 / cfg.tau_seconds)) * math.log(float(close / D('5'))) / cfg.psi)
    assert len(rt.closes) == 2
    for draws in engine._arrival_rngs.values():
        draws.arrivals = 0
    old_z = rt.z
    await engine._tick_pair(pid, cfg, now + timedelta(seconds=5))
    expected_rate = ((1 - cfg.activity_b) * cfg.mean_arrivals_per_20 + cfg.activity_b * old_z ** 2) / 20
    assert engine._arrival_rngs[random_bot.profile_id].rates[-1] == pytest.approx(expected_rate)


@pytest.mark.asyncio
async def test_restart_continues_persisted_episode_and_pause_stops_events():
    pid, cfg, bots = await seed([{'template': 'episode', 'count': 1}])
    bot = bots[0]
    now = datetime.now(timezone.utc)
    first = FxPveEngine()
    first._mark_rngs[bot.profile_id] = Draws()
    first._arrival_rngs[bot.profile_id] = Draws(arrivals=1)
    assert (await first._tick_pair(pid, cfg, now))['trade'] == 1
    async with async_session_maker() as db:
        profile = await db.get(BotProfile, bot.profile_id)
        assert profile.strategy_state['episode']['remaining'] == '342.000000'
        # A real pool price change makes the long profitable.
        pair = await db.get(FxPair, pid)
        pair.gold_reserve *= D('1.05')
        await db.commit()
    restarted = FxPveEngine()
    restarted._mark_rngs[bot.profile_id] = Draws()
    restarted._arrival_rngs[bot.profile_id] = Draws(arrivals=2)
    result = await restarted._tick_pair(pid, cfg, now + timedelta(seconds=5))
    assert result['trade'] == 1 and result['skip'] == 1
    async with async_session_maker() as db:
        profile = await db.get(BotProfile, bot.profile_id)
        assert profile.strategy_state['episode'] is None
        profile.status = 'paused'
        await db.commit()
    assert (await restarted._tick_pair(pid, cfg, now + timedelta(seconds=10)))['bots'] == 0
    assert len(restarted.pairs[pid].closes) == 3  # only real samples since restart
    assert len(restarted.get_log(bot.profile_id)) == 2


def test_inventory_ema_half_life_and_shared_tick_updates():
    from app.services.pve_fx.engine import PairRuntime
    rt = PairRuntime()
    rt.inventory_reference(600, math.log(5), 0)
    for tick in range(1, 121):
        value = rt.inventory_reference(600, math.log(10), tick)
        assert rt.inventory_reference(600, math.log(10), tick) == value
    assert math.exp(value) == pytest.approx(math.sqrt(50))


@pytest.mark.asyncio
async def test_inventory_ema_updates_without_arrivals_or_price_history():
    pid, cfg, bots = await seed([{'template': 'inventory', 'count': 2}])
    engine = FxPveEngine()
    for bot in bots:
        engine._arrival_rngs[bot.profile_id] = Draws(arrivals=0)
    now = datetime.now(timezone.utc)
    await engine._tick_pair(pid, cfg, now)
    async with async_session_maker() as db:
        pair = await db.get(FxPair, pid)
        pair.gold_reserve *= 2
        await db.commit()
    await engine._tick_pair(pid, cfg, now + timedelta(seconds=5))
    result = await engine._tick_pair(pid, cfg, now + timedelta(seconds=10))
    rt = engine.pairs[pid]
    reference = math.exp(rt.inventory_ema[600][0])
    assert 5 < reference < 10
    assert result['arrivals'] == result['trade'] == result['error'] == 0
    assert len(rt.closes) == rt.closes.maxlen == 1
