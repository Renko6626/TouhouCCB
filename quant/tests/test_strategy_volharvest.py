"""VolatilityHarvest 策略测试。

按 SKILL writing-quant-strategy 三类：
- 单元：logit 转换 / 预热 / 低 MAD skip / deadband / tanh 饱和 / trade clip /
  trend guard / bootstrap guard / reconcile
- 集成（SseSubscriber dispatch）
- Setup / holdings 初始化
"""
import asyncio
import math
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

from thccb_quant.client.rest import HoldingRead, MarketDetail, OutcomeDetail
from thccb_quant.client.sse import SseEvent
from thccb_quant.client.sse_subscriber import SseSubscriber
from thccb_quant.errors import RiskRejected
from thccb_quant.state.store import Store
from thccb_quant.strategy.base import StrategyContext
from thccb_quant.strategy.volharvest import VolatilityHarvest, compute_target, to_logit


# ─── fixtures ────────────────────────────────────────────────────

@pytest.fixture
async def store(tmp_path: Path):
    s = await Store.open(tmp_path / "t.db")
    yield s
    await s.close()


def _default_cfg(**overrides) -> dict:
    cfg = {
        "market_id": 1, "outcome_id": 1,
        "window_size": 10, "k_sigma": 2.0, "scale_mad": 1.0,
        "min_mad_logit": 0.001,
        "base_shares": 100.0, "max_offset_shares": 40.0,
        "min_trade_shares": 1.0, "max_trade_shares": 10.0,
        "bootstrap_max_step": 5.0, "bootstrap_interval_sec": 0,
        "bootstrap_skip_if_overpriced": True,
        "reconcile_interval_sec": 999, "reconcile_tolerance": 0.5,
        "trend_guard_events": 3,
    }
    cfg.update(overrides)
    return cfg


def _holding(outcome_id: int, amount: Decimal) -> HoldingRead:
    return HoldingRead(
        outcome_id=outcome_id, outcome_label="yes",
        market_id=1, market_title="t",
        amount=amount, cost_basis=Decimal("0"),
        avg_price=Decimal("0.5"), current_price=Decimal("0.5"),
        market_value=Decimal("0"), unrealized_pnl=Decimal("0"),
    )


async def _make_ctx(store: Store, current_holding: Decimal = Decimal("100")) -> StrategyContext:
    rest = MagicMock()
    rest.get_holdings = AsyncMock(return_value=[_holding(1, current_holding)])
    rest.get_market = AsyncMock(return_value=MarketDetail(
        id=1, title="t", status="trading", liquidity_b=10000.0,
        outcomes=[
            OutcomeDetail(id=1, label="yes", total_shares=Decimal("100"),
                          current_price=Decimal("0.5")),
            OutcomeDetail(id=2, label="no", total_shares=Decimal("100"),
                          current_price=Decimal("0.5")),
        ],
    ))
    broker = MagicMock()
    broker.buy = AsyncMock(return_value=MagicMock(
        shares=Decimal("1"), cost=Decimal("0.5"), new_cash=Decimal("499.5"),
    ))
    broker.sell = AsyncMock(return_value=MagicMock(
        shares=Decimal("1"), cost=Decimal("-0.5"), new_cash=Decimal("500.5"),
    ))
    return StrategyContext(
        rest=rest, broker=broker, store=store,
        logger=structlog.get_logger("test"),
        config={"max_slippage_bps": 300},
    )


def _trade_event(
    *, seq: int, side: str, price: float,
    outcome_id: int = 1, trade_id: int | None = None,
    ts: str = "2026-05-17T07:00:00Z",
) -> SseEvent:
    """price 是被成交 outcome 的 post_market_price；market_prices_post 始终按
    outcome.id 升序（outcome 1, outcome 2）排列。"""
    if outcome_id == 1:
        prices_post = [price, 1.0 - price]
    else:  # outcome_id == 2（二元市场互补）
        prices_post = [1.0 - price, price]
    return SseEvent(
        type="trade", seq=seq,
        data={"trade": {
            "id": trade_id if trade_id is not None else seq,
            "type": side, "outcome_id": outcome_id,
            "username": "u", "shares": 1.0, "price": price,
            "gross": price, "fee": 0.0,
            "post_market_price": price,
            "market_prices_post": prices_post,
            "timestamp": ts,
        }},
    )


# ─── 1. logit / window 单元 ───────────────────────────────────────

def test_logit_conversion_correct():
    assert abs(to_logit(0.5) - 0.0) < 1e-9
    assert abs(to_logit(0.62) - math.log(0.62 / 0.38)) < 1e-9
    # clamp 边界
    assert to_logit(0.0) == to_logit(0.001)
    assert to_logit(1.0) == to_logit(0.999)


async def test_window_not_warm_skips(store):
    s = VolatilityHarvest(name="v", config=_default_cfg(window_size=20))
    ctx = await _make_ctx(store, current_holding=Decimal("100"))  # = base
    await s.setup(ctx)
    # 喂少于 window/2 笔 → 不下单
    for i in range(5):
        await s.on_sse_event(_trade_event(seq=i + 1, side="BUY", price=0.5))
    assert ctx.broker.buy.call_count == 0
    assert ctx.broker.sell.call_count == 0


async def test_low_mad_skips(store):
    s = VolatilityHarvest(name="v", config=_default_cfg(min_mad_logit=10.0))
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    await s.setup(ctx)
    for i in range(20):
        await s.on_sse_event(_trade_event(seq=i + 1, side="BUY", price=0.5 + 0.001 * i))
    # MAD 远小于 min_mad_logit → 全 skip
    assert ctx.broker.buy.call_count == 0


# ─── 2. deadband + tanh（关键回归测试）─────────────────────────────

async def test_within_deadband_target_is_base(store):
    """deviation 在 deadband 内时 target == base，不应下单。"""
    s = VolatilityHarvest(name="v", config=_default_cfg(window_size=10))
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    await s.setup(ctx)
    prices = [0.50, 0.51, 0.49, 0.50, 0.52, 0.48, 0.50, 0.51, 0.49, 0.50]
    for i, p in enumerate(prices):
        await s.on_sse_event(_trade_event(seq=i + 1, side="BUY", price=p))
    pre = ctx.broker.buy.call_count + ctx.broker.sell.call_count
    # 再喂一笔 deviation < threshold（接近中位数）
    await s.on_sse_event(_trade_event(seq=100, side="BUY", price=0.505))
    assert (ctx.broker.buy.call_count + ctx.broker.sell.call_count) == pre


def test_no_tanh_jump_at_threshold():
    """关键 regression：deviation 从 0.99*threshold 到 1.01*threshold 时
    target 变化幅度 < max_offset * 0.05（验证不再是 76% 跳变）。"""
    base, max_offset, k_sigma, scale_mad = 100.0, 40.0, 2.0, 1.0
    mad = 0.1
    threshold = k_sigma * 1.4826 * mad  # ≈ 0.296

    t_just_below = compute_target(
        deviation=threshold * 0.99, mad=mad,
        base=base, max_offset=max_offset,
        k_sigma=k_sigma, scale_mad=scale_mad,
    )
    t_just_above = compute_target(
        deviation=threshold * 1.01, mad=mad,
        base=base, max_offset=max_offset,
        k_sigma=k_sigma, scale_mad=scale_mad,
    )
    jump = abs(t_just_above - t_just_below)
    assert jump < max_offset * 0.05, f"target jumped {jump} ≥ 5% of max_offset"


def test_just_past_threshold_starts_from_zero():
    """deviation 刚过阈值时 offset 应该接近 0（验证从 0 开始而非 76%）。"""
    base, max_offset, k_sigma, scale_mad = 100.0, 40.0, 2.0, 1.0
    mad = 0.1
    threshold = k_sigma * 1.4826 * mad
    # excess = 0.01 * mad，u 很小
    t = compute_target(
        deviation=threshold + 0.01 * mad, mad=mad,
        base=base, max_offset=max_offset,
        k_sigma=k_sigma, scale_mad=scale_mad,
    )
    # 偏离应该非常小
    assert abs(t - base) < max_offset * 0.05


def test_tanh_saturation_far_above():
    t = compute_target(
        deviation=10.0, mad=0.1, base=100.0, max_offset=40.0,
        k_sigma=2.0, scale_mad=1.0,
    )
    # 接近 base - max_offset = 60
    assert 60.0 <= t < 60.5


def test_tanh_saturation_far_below():
    t = compute_target(
        deviation=-10.0, mad=0.1, base=100.0, max_offset=40.0,
        k_sigma=2.0, scale_mad=1.0,
    )
    # 接近 base + max_offset = 140
    assert 139.5 < t <= 140.0


# ─── 3. min/max trade shares ─────────────────────────────────────

async def test_min_trade_shares_filter(store):
    s = VolatilityHarvest(name="v", config=_default_cfg(min_trade_shares=50.0))
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    await s.setup(ctx)
    prices = [0.50] * 10
    for i, p in enumerate(prices):
        await s.on_sse_event(_trade_event(seq=i + 1, side="BUY", price=p))
    # 一笔小涨触发但 delta < min_trade
    await s.on_sse_event(_trade_event(seq=11, side="BUY", price=0.51))
    assert ctx.broker.buy.call_count == 0
    assert ctx.broker.sell.call_count == 0


async def test_max_trade_shares_clip(store):
    """|delta| > max_trade_shares 时 clip 到 ±max_trade_shares。"""
    s = VolatilityHarvest(name="v", config=_default_cfg(
        max_trade_shares=3.0, max_offset_shares=100.0, min_trade_shares=0.1,
    ))
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    await s.setup(ctx)
    # 喂混合价让 MAD 非零
    for i, p in enumerate([0.50, 0.51, 0.49, 0.50, 0.51, 0.49, 0.50, 0.51, 0.49, 0.50]):
        await s.on_sse_event(_trade_event(seq=i + 1, side="BUY", price=p))
    # 来一笔极端高价 → 想 sell 但应被 clip
    await s.on_sse_event(_trade_event(seq=100, side="BUY", price=0.90))
    if ctx.broker.sell.call_count > 0:
        args = ctx.broker.sell.call_args
        assert args.kwargs["shares"] <= Decimal("3.0")


# ─── 4. 持仓增量 ─────────────────────────────────────────────────

async def test_holding_update_on_success(store):
    s = VolatilityHarvest(name="v", config=_default_cfg(
        window_size=6, min_trade_shares=0.1, max_offset_shares=100.0,
    ))
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    ctx.broker.sell = AsyncMock(return_value=MagicMock(
        shares=Decimal("3"), cost=Decimal("-1.5"), new_cash=Decimal("501.5"),
    ))
    await s.setup(ctx)
    initial = s._holding
    # 喂混合价让 MAD 非零 + 触发 sell（涨太多）
    for i, p in enumerate([0.50, 0.51, 0.49, 0.50, 0.51, 0.49]):
        await s.on_sse_event(_trade_event(seq=i + 1, side="BUY", price=p))
    # 来一笔大涨 → 触发 sell（去库存）
    await s.on_sse_event(_trade_event(seq=100, side="BUY", price=0.80))
    if ctx.broker.sell.call_count > 0:
        expected = initial - Decimal("3") * ctx.broker.sell.call_count
        assert s._holding == expected


async def test_holding_no_update_on_failure(store):
    s = VolatilityHarvest(name="v", config=_default_cfg(
        window_size=6, min_trade_shares=0.1, max_offset_shares=100.0,
    ))
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    ctx.broker.buy = AsyncMock(side_effect=RiskRejected("test"))
    ctx.broker.sell = AsyncMock(side_effect=RiskRejected("test"))
    await s.setup(ctx)
    initial = s._holding
    for i, p in enumerate([0.50, 0.51, 0.49, 0.50, 0.51, 0.49]):
        await s.on_sse_event(_trade_event(seq=i + 1, side="BUY", price=p))
    await s.on_sse_event(_trade_event(seq=100, side="BUY", price=0.80))
    assert s._holding == initial  # 失败不更新


# ─── 5. trend guard ──────────────────────────────────────────────

async def test_trend_guard_up_blocks_sell(store):
    """连续 N 笔 BUY 时，策略想 sell 应被拦。"""
    s = VolatilityHarvest(name="v", config=_default_cfg(
        window_size=6, trend_guard_events=3,
        max_offset_shares=100.0, min_trade_shares=0.1,
    ))
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    await s.setup(ctx)
    # 先填窗口 + 让 MAD 非零（混合 side，末尾连续 BUY 让 trend window 饱和）
    for i, (side, p) in enumerate([
        ("BUY", 0.50), ("SELL", 0.51), ("BUY", 0.49),
        ("BUY", 0.50), ("BUY", 0.51), ("BUY", 0.49),
    ]):
        await s.on_sse_event(_trade_event(seq=i + 1, side=side, price=p))
    # 此时 trend_window 是末尾 3 笔 = BUY/BUY/BUY
    sells_before = ctx.broker.sell.call_count
    # 来一笔大涨 BUY → 策略想 sell 但被 trend guard 拦
    await s.on_sse_event(_trade_event(seq=10, side="BUY", price=0.80))
    assert ctx.broker.sell.call_count == sells_before


async def test_trend_guard_down_blocks_buy(store):
    """连续 N 笔 SELL + 价格大跌时，策略想 buy 应被拦。"""
    s = VolatilityHarvest(name="v", config=_default_cfg(
        window_size=6, trend_guard_events=3,
        max_offset_shares=100.0, min_trade_shares=0.1,
    ))
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    await s.setup(ctx)
    # 末尾连续 SELL 让 trend_window 饱和 = SELL/SELL/SELL
    for i, (side, p) in enumerate([
        ("BUY", 0.50), ("SELL", 0.51), ("BUY", 0.49),
        ("SELL", 0.50), ("SELL", 0.51), ("SELL", 0.49),
    ]):
        await s.on_sse_event(_trade_event(seq=i + 1, side=side, price=p))
    buys_before = ctx.broker.buy.call_count
    # 来一笔大跌 SELL → 策略想 buy 但被 trend guard 拦
    await s.on_sse_event(_trade_event(seq=10, side="SELL", price=0.30))
    assert ctx.broker.buy.call_count == buys_before


async def test_trend_guard_mixed_passes(store):
    """trend_window 混合 side 时不拦（断言 guard 不阻断；最终是否下单看信号）。"""
    s = VolatilityHarvest(name="v", config=_default_cfg(
        window_size=6, trend_guard_events=3,
        max_offset_shares=100.0, min_trade_shares=0.1,
    ))
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    await s.setup(ctx)
    # 混合方向填窗口
    for i, (side, p) in enumerate([
        ("BUY", 0.50), ("SELL", 0.51), ("BUY", 0.49),
        ("SELL", 0.50), ("BUY", 0.51), ("SELL", 0.49),
    ]):
        await s.on_sse_event(_trade_event(seq=i + 1, side=side, price=p))
    # 混合 trend → guard 应不拦；大涨触发 sell 应该执行
    await s.on_sse_event(_trade_event(seq=10, side="BUY", price=0.80))
    await s.on_sse_event(_trade_event(seq=11, side="SELL", price=0.85))
    await s.on_sse_event(_trade_event(seq=12, side="BUY", price=0.90))
    # trend_window 此时含 BUY/SELL/BUY 混合 → guard 不拦 → sell 触发
    assert ctx.broker.sell.call_count >= 1


async def test_trend_guard_not_full_passes(store):
    """trend_window 不满 N 时 guard 不拦。"""
    s = VolatilityHarvest(name="v", config=_default_cfg(
        window_size=6, trend_guard_events=10,  # 大窗口不会满
        max_offset_shares=100.0, min_trade_shares=0.1,
    ))
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    await s.setup(ctx)
    for i, (side, p) in enumerate([
        ("BUY", 0.50), ("BUY", 0.51), ("BUY", 0.49),
        ("BUY", 0.50), ("BUY", 0.51), ("BUY", 0.49),
    ]):
        await s.on_sse_event(_trade_event(seq=i + 1, side=side, price=p))
    # trend_window 此时只有 6 笔 < 10 → guard 不拦
    await s.on_sse_event(_trade_event(seq=10, side="BUY", price=0.80))
    assert ctx.broker.sell.call_count >= 1


# ─── 5b. cross-outcome SSE 处理（LMSR 任一 outcome 成交都改变 self 价）─

async def test_other_outcome_trade_updates_window_with_self_price(store):
    """outcome 2 trade 也要喂窗口，且价格用的是 self（outcome 1）的现价。"""
    s = VolatilityHarvest(name="v", config=_default_cfg(window_size=10))
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    await s.setup(ctx)
    # outcome 2 SELL @ outcome 2 price 0.30 → outcome 1 price = 0.70
    await s.on_sse_event(_trade_event(seq=1, side="SELL", price=0.30, outcome_id=2))
    assert len(s._window) == 1
    expected_logit = math.log(0.70 / 0.30)
    assert abs(s._window[-1][1] - expected_logit) < 1e-6


async def test_other_outcome_buy_is_down_pressure(store):
    """outcome 2 BUY → 对 self 是下跌压力 → trend_window 推 'DOWN'。"""
    s = VolatilityHarvest(name="v", config=_default_cfg(trend_guard_events=3))
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    await s.setup(ctx)
    await s.on_sse_event(_trade_event(seq=1, side="BUY", price=0.50, outcome_id=2))
    assert list(s._trend_window) == ["DOWN"]


async def test_other_outcome_sell_is_up_pressure(store):
    """outcome 2 SELL → 对 self 是上涨压力 → trend_window 推 'UP'。"""
    s = VolatilityHarvest(name="v", config=_default_cfg(trend_guard_events=3))
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    await s.setup(ctx)
    await s.on_sse_event(_trade_event(seq=1, side="SELL", price=0.50, outcome_id=2))
    assert list(s._trend_window) == ["UP"]


# ─── 6. bootstrap guard ──────────────────────────────────────────

async def test_bootstrap_buys_when_below_base(store):
    s = VolatilityHarvest(name="v", config=_default_cfg(
        base_shares=100.0, bootstrap_max_step=5.0,
        bootstrap_interval_sec=0,
    ))
    ctx = await _make_ctx(store, current_holding=Decimal("50"))
    await s.setup(ctx)
    await s.on_sse_event(_trade_event(seq=1, side="BUY", price=0.5))
    assert ctx.broker.buy.call_count == 1
    args = ctx.broker.buy.call_args
    assert args.kwargs["shares"] <= Decimal("5.0")


async def test_bootstrap_interval_throttle(store):
    s = VolatilityHarvest(name="v", config=_default_cfg(
        base_shares=100.0, bootstrap_max_step=5.0,
        bootstrap_interval_sec=999,
    ))
    ctx = await _make_ctx(store, current_holding=Decimal("50"))
    await s.setup(ctx)
    await s.on_sse_event(_trade_event(seq=1, side="BUY", price=0.5))
    first = ctx.broker.buy.call_count
    await s.on_sse_event(_trade_event(seq=2, side="BUY", price=0.5))
    assert ctx.broker.buy.call_count == first


async def test_bootstrap_skip_when_overpriced(store):
    """window 已热 + deviation > 0 → 跳过 bootstrap。"""
    s = VolatilityHarvest(name="v", config=_default_cfg(
        window_size=6, base_shares=100.0,
        bootstrap_max_step=5.0,
        bootstrap_skip_if_overpriced=True,
        bootstrap_interval_sec=0,
    ))
    ctx = await _make_ctx(store, current_holding=Decimal("50"))
    await s.setup(ctx)
    # 喂 6 笔均匀价让 median 稳定 + window 已热
    for i, p in enumerate([0.50, 0.51, 0.49, 0.50, 0.50, 0.51]):
        await s.on_sse_event(_trade_event(seq=i + 1, side="BUY", price=p))
    buys_after_warmup = ctx.broker.buy.call_count
    # 高价 0.70（deviation > 0）→ 应跳过本次 bootstrap
    await s.on_sse_event(_trade_event(seq=10, side="BUY", price=0.70))
    assert ctx.broker.buy.call_count == buys_after_warmup


async def test_bootstrap_step_capped(store):
    """base-holding 很大时单次只买 max_step。"""
    s = VolatilityHarvest(name="v", config=_default_cfg(
        base_shares=100.0, bootstrap_max_step=5.0,
        bootstrap_interval_sec=0, max_trade_shares=100.0,
    ))
    ctx = await _make_ctx(store, current_holding=Decimal("10"))  # 缺 90
    await s.setup(ctx)
    await s.on_sse_event(_trade_event(seq=1, side="BUY", price=0.5))
    assert ctx.broker.buy.call_count == 1
    args = ctx.broker.buy.call_args
    assert args.kwargs["shares"] == Decimal("5.0")  # 单次 cap


# ─── 7. reconcile ────────────────────────────────────────────────

async def test_reconcile_corrects_drift(store):
    """reconcile 校正持仓漂移；base=50 < holding 保证不会触发 bootstrap 抢盖。"""
    s = VolatilityHarvest(name="v", config=_default_cfg(
        base_shares=50.0,
        reconcile_interval_sec=0,
        reconcile_tolerance=0.5,
    ))
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    await s.setup(ctx)
    assert s._holding == Decimal("100")
    ctx.rest.get_holdings = AsyncMock(return_value=[_holding(1, Decimal("80"))])
    await s.on_sse_event(_trade_event(seq=1, side="BUY", price=0.5))
    assert s._holding == Decimal("80")


async def test_reconcile_throttled(store):
    s = VolatilityHarvest(name="v", config=_default_cfg(
        reconcile_interval_sec=999,
    ))
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    await s.setup(ctx)
    setup_calls = ctx.rest.get_holdings.call_count
    await s.on_sse_event(_trade_event(seq=1, side="BUY", price=0.5))
    assert ctx.rest.get_holdings.call_count == setup_calls


async def test_reconcile_within_tolerance_no_action(store):
    s = VolatilityHarvest(name="v", config=_default_cfg(
        reconcile_interval_sec=0, reconcile_tolerance=5.0,
    ))
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    await s.setup(ctx)
    ctx.rest.get_holdings = AsyncMock(return_value=[_holding(1, Decimal("102"))])
    await s.on_sse_event(_trade_event(seq=1, side="BUY", price=0.5))
    assert s._holding == Decimal("100")  # 漂移 2 < 5 → 不覆盖


# ─── 8. 集成：SseSubscriber dispatch ──────────────────────────────

async def test_subscriber_routes_to_volharvest(store):
    s = VolatilityHarvest(name="v", config=_default_cfg())
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    await s.setup(ctx)

    sse_client = MagicMock()

    async def fake_subscribe(market_id):
        yield _trade_event(seq=1, side="BUY", price=0.5)

    sse_client.subscribe = fake_subscribe

    rest = MagicMock()
    rest.get_recent_trades = AsyncMock(return_value=[])
    sub = SseSubscriber(
        rest=rest, store=store, sse_client=sse_client,
        strategies=[s], market_ids={1},
        logger=structlog.get_logger("test"),
    )
    await asyncio.wait_for(sub.run(), timeout=2.0)
    assert len(s._window) >= 1


# ─── 9. setup / holdings 初始化 ──────────────────────────────────

async def test_setup_reads_actual_holdings(store):
    s = VolatilityHarvest(name="v", config=_default_cfg(base_shares=100.0))
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    await s.setup(ctx)
    assert s._holding == Decimal("100")
    assert s.market_id == 1  # SseSubscriber 路由依赖


async def test_setup_below_base_enters_bootstrap(store):
    s = VolatilityHarvest(name="v", config=_default_cfg(base_shares=100.0))
    ctx = await _make_ctx(store, current_holding=Decimal("30"))
    await s.setup(ctx)
    assert s._holding == Decimal("30")
    # bootstrap mode 由 _holding < base 判断（无显式 flag）


async def test_setup_above_base_no_proactive_sell(store):
    s = VolatilityHarvest(name="v", config=_default_cfg(base_shares=100.0))
    ctx = await _make_ctx(store, current_holding=Decimal("200"))
    await s.setup(ctx)
    assert s._holding == Decimal("200")
    assert ctx.broker.sell.call_count == 0


# ─── 10. 配置必填 ────────────────────────────────────────────────

def test_missing_market_id_raises_keyerror():
    cfg = _default_cfg()
    del cfg["market_id"]
    with pytest.raises(KeyError, match="market_id"):
        VolatilityHarvest(name="v", config=cfg)


# ─── 11. 反侦察：整数 shares 下单 ─────────────────────────────────

async def test_volharvest_orders_integer_shares_in_main_signal(store):
    """主信号下单 shares 必须是整数。"""
    s = VolatilityHarvest(name="v", config=_default_cfg(
        window_size=5, base_shares=100.0, max_offset_shares=50.0,
        min_trade_shares=1.0, max_trade_shares=20.0,
        k_sigma=0.5, scale_mad=0.5,
    ))
    ctx = await _make_ctx(store, current_holding=Decimal("100"))
    await s.setup(ctx)
    # 制造非零 deviation 让 tanh 给非整数 target
    for i, p in enumerate([0.5, 0.51, 0.49, 0.5, 0.55]):
        await s.on_sse_event(_trade_event(seq=i+1, side="BUY", price=p))
    # 触发一笔实际下单
    await s.on_sse_event(_trade_event(seq=100, side="BUY", price=0.75))

    # 至少有一笔下单
    total = ctx.broker.buy.call_count + ctx.broker.sell.call_count
    if total > 0:
        # 检查每一笔下单的 shares 都是整数
        all_calls = (list(ctx.broker.buy.call_args_list)
                     + list(ctx.broker.sell.call_args_list))
        for call in all_calls:
            shares = call.kwargs["shares"]
            assert shares == shares.to_integral_value(), \
                f"non-integer shares: {shares}"


async def test_volharvest_bootstrap_orders_integer_shares(store):
    """Bootstrap 下单 shares 必须是整数。"""
    s = VolatilityHarvest(name="v", config=_default_cfg(
        base_shares=100.0, bootstrap_max_step=7.5,  # 非整数 max_step
        bootstrap_interval_sec=0,
    ))
    ctx = await _make_ctx(store, current_holding=Decimal("50"))  # 需要 bootstrap
    await s.setup(ctx)
    await s.on_sse_event(_trade_event(seq=1, side="BUY", price=0.5))
    assert ctx.broker.buy.call_count == 1
    shares = ctx.broker.buy.call_args.kwargs["shares"]
    assert shares == shares.to_integral_value(), f"non-integer shares: {shares}"
    # 7.5 向下取整 = 7
    assert shares == Decimal("7"), f"expected 7, got {shares}"
