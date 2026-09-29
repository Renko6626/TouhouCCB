"""Background FX target, noise, and macro intervention engine.

统一信贷（WP6b，flag=unified_credit_enabled）：
- 每个 pair 用 pair 独占门闩 + 独立短事务处理（不同 pair 互不延长事务，也不在持
  行锁时等待门闩）；真实改池（有成交）时 `pair.pool_version += 1`，风险快照据此失效；
- `reduce_only` pair 禁止系统干预（不随机游走、不目标干预、不噪声单、不事件冲击），
  paused / draft / closed 交集本就只选 `trading`，保持全停；
- 开关关闭时走 legacy 单事务整批路径，行为逐字段不变。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_maker
from app.models.fx import FxEvent, FxPair, FxTrade, FxTreasury
from app.services import audit_service, site_config
from app.services.credit import flags as credit_flags
from app.services.credit.gates import GATES
from app.services.credit.keys import GroupKey
from app.services.credit.ownership import OWNERSHIP
from app.services.fx.amm import quote_buy, quote_sell, marginal_price
from app.services.fx.quantize import amount_down
from app.services.fx.randomness import FxRandomSource


ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True)
class FxTickResult:
    pairs: int = 0
    noise_orders: int = 0
    intervention_orders: int = 0
    skipped: int = 0
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class _TickConfig:
    """一次 tick 的 site_config 快照（legacy 整批读一次；gated 每 pair 读一次）。"""

    sigma: Decimal
    step_max: Decimal
    pool_ratio: Decimal
    half_life: int
    daily_budget: Decimal
    move_limit: Decimal


def _utc(value: Optional[datetime] = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _json_decimal(value: Decimal) -> str:
    return format(value, "f")


def event_progress(elapsed_sec: float, window_sec: int) -> Decimal:
    """Fraction of the first-to-final event gap applied at elapsed time."""
    if window_sec <= 0 or elapsed_sec <= 0:
        return ZERO
    dt = min(float(elapsed_sec), float(window_sec))
    return Decimal(str(1 - math.exp(-math.log(20) * dt / float(window_sec))))


def normal_target_progress(elapsed_sec: float, half_life_sec: int) -> Decimal:
    if half_life_sec <= 0 or elapsed_sec <= 0:
        return ZERO
    return Decimal(str(1 - math.exp(-math.log(2) * elapsed_sec / half_life_sec)))


def normal_intervention_price(current: Decimal, target: Decimal, elapsed_sec: float,
                              half_life_sec: int, max_move: Decimal = Decimal("0.005")) -> Decimal:
    if current <= 0 or target <= 0:
        return current
    p = normal_target_progress(elapsed_sec, half_life_sec)
    desired = current * (target / current).ln().exp() ** p
    cap = current * max_move
    return max(current - cap, min(current + cap, desired))


class FxEngine:
    def __init__(self, session_factory=async_session_maker):
        self.session_factory = session_factory
        self._last_target_at: dict[int, datetime] = {}
        self._last_noise_at: dict[int, datetime] = {}
        self._next_noise_at: dict[int, datetime] = {}

    async def _safe_system_move(self, db: AsyncSession, pair: FxPair, treasury: FxTreasury,
                                desired: Decimal, event_budget: Decimal, daily_budget: Decimal,
                                *, source: str, now: datetime, max_ratio: Optional[Decimal] = None):
        try:
            async with db.begin_nested():
                return await self._system_move(db, pair, treasury, desired, event_budget,
                                               daily_budget, source=source, now=now,
                                               max_ratio=max_ratio)
        except Exception as exc:
            return _NoMove(f"exception:{type(exc).__name__}")

    async def _load_tick_config(self, db: AsyncSession) -> _TickConfig:
        return _TickConfig(
            sigma=await site_config.get_decimal_or(db, "fx_hourly_sigma", Decimal("0.002")),
            step_max=await site_config.get_decimal_or(db, "fx_step_max_ratio", Decimal("0.001")),
            pool_ratio=await site_config.get_decimal_or(db, "fx_noise_pool_ratio", Decimal("0.0001")),
            half_life=await site_config.get_int_or(db, "fx_system_half_life_sec", 600),
            daily_budget=await site_config.get_decimal_or(db, "fx_daily_budget", Decimal("100000")),
            move_limit=await site_config.get_decimal_or(
                db, "fx_default_price_move_limit", Decimal("0.005")),
        )

    async def tick(self, now: Optional[datetime] = None, rng: Any = None) -> FxTickResult:
        if OWNERSHIP.reason is not None:
            OWNERSHIP.require_writes()
        now = _utc(now)
        source = rng or random
        if credit_flags.get_flags().unified_credit_enabled:
            return await self._tick_gated(now, source)
        return await self._tick_legacy(now, source)

    async def _tick_legacy(self, now: datetime, source: Any) -> FxTickResult:
        """flag OFF 原路径：单 session / 单 commit 处理全部 trading pair。"""
        created_trades: list[FxTrade] = []
        async with self.session_factory() as db:
            if not await site_config.get_bool_or(db, "fx_enabled", False):
                return FxTickResult(reasons=("gate_off",))
            pairs = (await db.execute(select(FxPair).where(FxPair.status == "trading").with_for_update())).scalars().all()
            reasons: list[str] = []
            noise_count = intervention_count = skipped = 0
            cfg = await self._load_tick_config(db)
            for pair in pairs:
                trades, noise, intervention, pair_skipped = await self._process_pair(
                    db, pair, cfg, now, source, reasons,
                )
                created_trades.extend(trades)
                noise_count += noise
                intervention_count += intervention
                skipped += pair_skipped
            await db.commit()
        for trade in created_trades:
            try:
                from app.services.fx.market_data import publish_trade
                await publish_trade(trade)
            except Exception:
                pass
        return FxTickResult(pairs=len(pairs), noise_orders=noise_count,
                            intervention_orders=intervention_count, skipped=skipped,
                            reasons=tuple(reasons))

    async def _tick_gated(self, now: datetime, source: Any) -> FxTickResult:
        """统一模式：每 pair「独占门闩 → 短事务」，pair 之间互不阻塞。"""
        OWNERSHIP.require_writes()
        reasons: list[str] = []
        noise_count = intervention_count = skipped = pairs_seen = 0
        async with self.session_factory() as db:
            if not await site_config.get_bool_or(db, "fx_enabled", False):
                return FxTickResult(reasons=("gate_off",))
            pair_ids = [int(x) for x in (await db.execute(
                select(FxPair.id).where(FxPair.status == "trading")
            )).scalars().all()]
        for pair_id in pair_ids:
            async with GATES.hold(exclusive=[GroupKey("fx", pair_id)]):
                async with self.session_factory() as db:
                    pair = (await db.execute(
                        select(FxPair)
                        .where(FxPair.id == pair_id, FxPair.status == "trading")
                        .with_for_update().execution_options(populate_existing=True)
                    )).scalars().first()
                    if pair is None:
                        # 等门闩期间状态被改走（paused/closed）：安全跳过
                        continue
                    pairs_seen += 1
                    if bool(pair.reduce_only):
                        # reduce-only：不许买入，也不许任何系统干预
                        skipped += 1
                        reasons.append(f"pair:{pair_id}:reduce_only")
                        continue
                    cfg = await self._load_tick_config(db)
                    try:
                        trades, noise, intervention, pair_skipped = await self._process_pair(
                            db, pair, cfg, now, source, reasons,
                        )
                        await db.commit()
                    except Exception as exc:
                        await db.rollback()
                        reasons.append(f"pair:{pair_id}:exception:{type(exc).__name__}")
                        continue
                    noise_count += noise
                    intervention_count += intervention
                    skipped += pair_skipped
                    for trade in trades:
                        try:
                            from app.services.fx.market_data import publish_trade
                            await publish_trade(trade)
                        except Exception:
                            pass
        return FxTickResult(pairs=pairs_seen, noise_orders=noise_count,
                            intervention_orders=intervention_count, skipped=skipped,
                            reasons=tuple(reasons))

    async def _process_pair(self, db: AsyncSession, pair: FxPair, cfg: _TickConfig,
                            now: datetime, source: Any, reasons: list[str],
                            ) -> tuple[list[FxTrade], int, int, int]:
        """单个 trading pair 的完整 tick 逻辑（legacy / gated 共用）。

        返回 ``(trades, noise_count, intervention_count, skipped)``；``reasons`` 就地追加。
        """
        trades: list[FxTrade] = []
        noise_count = intervention_count = skipped = 0
        treasury = (await db.execute(select(FxTreasury).where(FxTreasury.pair_id == pair.id).with_for_update())).scalars().first()
        OWNERSHIP.require_writes()
        if treasury is None:
            treasury = FxTreasury(pair_id=pair.id)
            db.add(treasury)
            await db.flush()
        if treasury.spend_date != now.date():
            treasury.spend_date, treasury.daily_spend = now.date(), ZERO
        budget_blocked = treasury.daily_spend >= cfg.daily_budget
        if budget_blocked:
            skipped += 1; reasons.append(f"pair:{pair.id}:daily_budget")
        active = (await db.execute(select(FxEvent).where(
            FxEvent.pair_id == pair.id, FxEvent.status == "published"
        ).order_by(FxEvent.published_at.asc()).with_for_update())).scalars().all()
        OWNERSHIP.require_writes()
        # Deterministic target random walk, bounded by pair's configured range and step cap.
        old_target = Decimal(pair.target_price)
        target_at = self._last_target_at.get(pair.id, _utc(pair.updated_at))
        elapsed = max(0.0, (now - target_at).total_seconds())
        target = old_target if active else (FxRandomSource.step_target(old_target.ln(), elapsed, cfg.sigma, source)).exp()
        reference = Decimal(getattr(pair, "initial_price", None) or old_target)
        lower = max(Decimal(pair.target_min), reference * Decimal("0.5"))
        upper = min(Decimal(pair.target_max), reference * Decimal("2"))
        if lower > upper:
            skipped += 1
            reasons.append(f"pair:{pair.id}:invalid_target_bounds")
            return trades, noise_count, intervention_count, skipped
        target = max(lower, min(upper, target))
        max_delta = old_target * cfg.step_max
        target = max(old_target - max_delta, min(old_target + max_delta, target))
        pair.target_price = max(lower, min(upper, amount_down(target)))
        pair.updated_at = now
        self._last_target_at[pair.id] = now

        normal_move = None
        if not active and not budget_blocked:
            current_price = marginal_price(pair.gold_reserve, pair.foreign_reserve)
            desired_price = normal_intervention_price(current_price, pair.target_price, elapsed, cfg.half_life,
                                                      max_move=cfg.move_limit)
            try:
                normal_move = await self._safe_system_move(db, pair, treasury, desired_price, ZERO,
                                                            cfg.daily_budget, source="target", now=now,
                                                            max_ratio=cfg.move_limit)
            except Exception as exc:
                normal_move = _NoMove(f"exception:{type(exc).__name__}")
                reasons.append(f"pair:{pair.id}:{normal_move.reason}")
            if normal_move:
                trades.append(normal_move.trade)
            elif isinstance(normal_move, _NoMove):
                reasons.append(f"pair:{pair.id}:normal:{normal_move.reason}")

        for event in active:
            elapsed_event = max(0.0, (now - _utc(event.published_at)).total_seconds())
            window = int(event.window_sec or 600)
            # Event intervention follows exponential decay toward the normal target.
            spent = Decimal(str((event.parameter_snapshot or {}).get("spent", "0")))
            remaining_budget = max(ZERO, Decimal(event.budget or 0) - spent)
            if remaining_budget <= 0:
                reasons.append(f"pair:{pair.id}:event:{event.id}:event_budget_exhausted")
                event.error_message = "event budget exhausted"
                event.status, event.completed_at = "completed", now
                audit_service.record(db, "fx_event_complete", ref_table="fx_event", ref_id=event.id,
                                      payload={"pair_id": pair.id, "reason": "budget_exhausted"})
                continue
            target_after = Decimal((event.parameter_snapshot or {}).get("target_after", old_target))
            previous_tick = (event.parameter_snapshot or {}).get("last_tick_at")
            previous_at = _utc(datetime.fromisoformat(previous_tick)) if previous_tick else _utc(event.published_at)
            dt = max(0.0, (now - previous_at).total_seconds())
            progress = event_progress(dt, window)
            current_price = marginal_price(pair.gold_reserve, pair.foreign_reserve)
            desired = current_price * ((target_after / current_price).ln() * progress).exp()
            try:
                moved = await self._safe_system_move(db, pair, treasury, desired, remaining_budget,
                                                     cfg.daily_budget, source="event", now=now)
            except Exception as exc:
                moved = _NoMove(f"exception:{type(exc).__name__}")
            if moved:
                intervention_count += 1
                trades.append(moved.trade)
                event.parameter_snapshot = dict(event.parameter_snapshot or {})
                event.parameter_snapshot["spent"] = str(spent + moved.actual_spend)
                event.parameter_snapshot["last_trade_id"] = moved.trade.id
                event.parameter_snapshot["last_tick_at"] = now.isoformat()
            elif isinstance(moved, _NoMove):
                reasons.append(f"pair:{pair.id}:event:{event.id}:{moved.reason}")
                event.error_message = f"event intervention failed: {moved.reason}"
                event.status, event.completed_at = "completed", now
                audit_service.record(db, "fx_event_complete", ref_table="fx_event", ref_id=event.id,
                                      payload={"pair_id": pair.id, "reason": event.error_message})
                continue
            else:
                event.error_message = f"event intervention failed: {getattr(moved, 'reason', 'unknown')}"
                event.status, event.completed_at = "completed", now
                audit_service.record(db, "fx_event_complete", ref_table="fx_event", ref_id=event.id,
                                      payload={"pair_id": pair.id, "reason": event.error_message})
                continue
            if elapsed_event >= window:
                event.status, event.completed_at = "completed", now
                audit_service.record(db, "fx_event_complete", ref_table="fx_event", ref_id=event.id,
                                      payload={"pair_id": pair.id})
        # Small zero-fee noise order, capped by configured pool ratio.
        noise_interval = await site_config.get_int_or(db, "fx_noise_interval_sec", 30)
        if pair.id not in self._next_noise_at:
            draw = source.expovariate(1.0 / max(1, noise_interval)) if hasattr(source, "expovariate") else random.expovariate(1.0 / max(1, noise_interval))
            self._next_noise_at[pair.id] = now + timedelta(seconds=draw)
        if now >= self._next_noise_at[pair.id]:
            current_price = marginal_price(pair.gold_reserve, pair.foreign_reserve)
            direction = ONE if (source.random() if hasattr(source, "random") else random.random()) >= 0.5 else -ONE
            desired = current_price * (ONE + direction * cfg.pool_ratio)
            try:
                moved = await self._safe_system_move(db, pair, treasury, desired, ZERO, cfg.daily_budget,
                                                     source="noise", now=now, max_ratio=cfg.pool_ratio)
            except Exception as exc:
                moved = _NoMove(f"exception:{type(exc).__name__}")
                reasons.append(f"pair:{pair.id}:{moved.reason}")
            noise_count += int(bool(moved))
            if moved:
                draw = source.expovariate(1.0 / max(1, noise_interval)) if hasattr(source, "expovariate") else random.expovariate(1.0 / max(1, noise_interval))
                self._next_noise_at[pair.id] = now + timedelta(seconds=draw)
                trades.append(moved.trade)
            elif isinstance(moved, _NoMove):
                reasons.append(f"pair:{pair.id}:noise:{moved.reason}")
        return trades, noise_count, intervention_count, skipped

    async def _system_move(self, db: AsyncSession, pair: FxPair, treasury: FxTreasury,
                           desired: Decimal, event_budget: Decimal, daily_budget: Decimal,
                           *, source: str, now: datetime, max_ratio: Optional[Decimal] = None) -> Optional[_MoveResult | _NoMove]:
        price = marginal_price(pair.gold_reserve, pair.foreign_reserve)
        if price <= 0 or desired <= 0:
            return _NoMove("invalid_price")
        if treasury.daily_spend >= daily_budget:
            return _NoMove("daily_budget_exhausted")
        ratio = desired / price - ONE
        if abs(ratio) < Decimal("0.0000005"):
            return _NoMove("below_tick_threshold")
        side = "buy" if ratio > 0 else "sell"
        cap = max_ratio or Decimal("1")
        if side == "buy":
            amount = pair.gold_reserve * ((desired / price).sqrt() - ONE)
            amount = min(pair.gold_reserve * cap, amount)
            if treasury.gold_balance < amount:
                amount = max(ZERO, treasury.gold_balance)
            if event_budget > 0: amount = min(amount, event_budget)
            amount = amount_down(min(amount, max(ZERO, daily_budget - treasury.daily_spend)))
            if amount <= 0: return _NoMove("gold_treasury_empty")
            try:
                q = quote_buy(amount, pair.gold_reserve, pair.foreign_reserve, ZERO)
            except (TypeError, ValueError, ArithmeticError):
                return _NoMove("quote_failed")
        else:
            amount = pair.foreign_reserve * ((price / desired).sqrt() - ONE)
            amount = min(pair.foreign_reserve * cap, amount)
            if event_budget > 0:
                amount = min(amount, event_budget / max(price, Decimal("0.000001")))
            amount = amount_down(min(amount, max(ZERO, daily_budget - treasury.daily_spend) / price))
            if amount <= 0: return _NoMove("foreign_treasury_empty")
            try:
                q = quote_sell(amount, pair.gold_reserve, pair.foreign_reserve, ZERO)
            except (TypeError, ValueError, ArithmeticError):
                return _NoMove("quote_failed")
        spend_gold = amount if side == "buy" else amount * price
        if treasury.daily_spend + spend_gold > daily_budget:
            return _NoMove("daily_budget_exhausted")
        OWNERSHIP.require_writes()
        if side == "buy":
            treasury.gold_balance -= amount
            treasury.foreign_balance += q.output_amount
        else:
            if treasury.foreign_balance < amount:
                return _NoMove("foreign_treasury_empty")
            treasury.foreign_balance -= amount
            treasury.gold_balance += q.output_amount
        pre_g, pre_f = pair.gold_reserve, pair.foreign_reserve
        pair.gold_reserve, pair.foreign_reserve = q.post_gold_reserve, q.post_foreign_reserve
        pair.pool_version += 1; pair.updated_at = now
        treasury.daily_spend += spend_gold; treasury.updated_at = now
        trade = FxTrade(pair_id=pair.id, user_id=None, side=side, input_amount=amount,
                        output_amount=q.output_amount, fee_amount=ZERO,
                        pre_gold_reserve=pre_g, pre_foreign_reserve=pre_f,
                        post_gold_reserve=q.post_gold_reserve, post_foreign_reserve=q.post_foreign_reserve,
                        post_price=q.post_price, source=f"system_{source}")
        db.add(trade); await db.flush()
        # System orders use the same replayable FX payload as player trades.
        # There is no user wallet, but pool and treasury snapshots are required
        # to verify both currencies independently.
        audit_service.record_fx_trade(db, trade=trade, user=None, pair=pair,
                                      wallet=None, treasury=treasury)
        return _MoveResult(trade=trade, actual_spend=spend_gold)


@dataclass(frozen=True)
class _MoveResult:
    trade: FxTrade
    actual_spend: Decimal


@dataclass(frozen=True)
class _NoMove:
    reason: str

    def __bool__(self) -> bool:
        return False
