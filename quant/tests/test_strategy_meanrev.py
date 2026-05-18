"""MeanRev 策略测试。

按 SKILL writing-quant-strategy 三类：
- 单元：logit + config 校验 / setup / warmup / deadband / sell-first / buy-fallback /
  sizing / cap / size_below_min / risk_rejected
- 集成：SseSubscriber dispatch
- Replay：不需要——_ema_logit 不持久化，持仓从 rest.get_holdings bootstrap
"""
import asyncio
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

from thccb_quant.client.rest import (
    HoldingRead,
    MarketDetail,
    OutcomeDetail,
    UserSummary,
)
from thccb_quant.client.sse import SseEvent
from thccb_quant.client.sse_subscriber import SseSubscriber
from thccb_quant.errors import RiskRejected
from thccb_quant.state.store import Store
from thccb_quant.strategy.base import StrategyContext
from thccb_quant.strategy.meanrev import MeanRevStrategy, to_logit


# ─── fixtures ────────────────────────────────────────────────────

@pytest.fixture
async def store(tmp_path: Path):
    s = await Store.open(tmp_path / "t.db")
    yield s
    await s.close()


def _default_cfg(**overrides) -> dict:
    cfg = {
        "market_id": 1,
        "ema_alpha": 0.5,        # 测试用大 alpha 让 EMA 跟得快
        "threshold_logit": 0.15,
        "warmup_events": 3,       # 测试用短 warmup
        "trade_pct_of_cash": 0.10,
        "size_scale_cap": 1.0,    # 默认关闭缩放，保留旧测试语义；缩放专用 case 显式 override
        "min_trade": 1,
        "max_holding_per_side": 200,
    }
    cfg.update(overrides)
    return cfg


def _holding(outcome_id: int, amount: Decimal) -> HoldingRead:
    return HoldingRead(
        outcome_id=outcome_id, outcome_label="O",
        market_id=1, market_title="t",
        amount=amount, cost_basis=Decimal("0"),
        avg_price=Decimal("0.5"), current_price=Decimal("0.5"),
        market_value=Decimal("0"), unrealized_pnl=Decimal("0"),
    )


def _summary(cash: Decimal) -> UserSummary:
    return UserSummary(
        cash=cash, debt=Decimal("0"),
        holdings_value=Decimal("0"), net_worth=cash,
    )


async def _make_ctx(
    store: Store, *,
    holding_a: Decimal = Decimal("0"),
    holding_b: Decimal = Decimal("0"),
    cash: Decimal = Decimal("500"),
    sell_shares_resp: Decimal = Decimal("10"),
    buy_shares_resp: Decimal = Decimal("10"),
) -> StrategyContext:
    rest = MagicMock()
    rest.get_market = AsyncMock(return_value=MarketDetail(
        id=1, title="t", status="trading", liquidity_b=10000.0,
        outcomes=[
            OutcomeDetail(id=1, label="A", total_shares=Decimal("100"),
                          current_price=Decimal("0.5")),
            OutcomeDetail(id=2, label="B", total_shares=Decimal("100"),
                          current_price=Decimal("0.5")),
        ],
    ))
    rest.get_holdings = AsyncMock(return_value=[
        _holding(1, holding_a), _holding(2, holding_b),
    ])
    rest.get_user_summary = AsyncMock(return_value=_summary(cash))
    broker = MagicMock()
    broker.buy = AsyncMock(return_value=MagicMock(
        shares=buy_shares_resp,
        cost=Decimal("5"),
        new_cash=cash - Decimal("5"),
    ))
    broker.sell = AsyncMock(return_value=MagicMock(
        shares=sell_shares_resp,
        cost=Decimal("-5"),
        new_cash=cash + Decimal("5"),
    ))
    return StrategyContext(
        rest=rest, broker=broker, store=store,
        logger=structlog.get_logger("test"),
        config={"max_slippage_bps": 300},
    )


def _trade_event(*, seq: int, price_a: float) -> SseEvent:
    """binary 市场：market_prices_post = [price_A, 1 - price_A]"""
    return SseEvent(
        type="trade", seq=seq,
        data={"trade": {
            "id": seq, "type": "BUY", "outcome_id": 1,
            "username": "u", "shares": 1.0, "price": price_a,
            "gross": price_a, "fee": 0.0,
            "post_market_price": price_a,
            "market_prices_post": [price_a, 1.0 - price_a],
            "timestamp": "2026-05-18T07:00:00Z",
        }},
    )


# ─── 1. 单元：logit + config ──────────────────────────────────────

def test_logit_basic():
    assert abs(to_logit(0.5)) < 1e-9


def test_logit_clamp():
    # 0 和 1 都会被 clamp，结果有限
    assert to_logit(0.0) == to_logit(0.001)
    assert to_logit(1.0) == to_logit(0.999)


def test_config_validation():
    with pytest.raises(ValueError, match="ema_alpha"):
        MeanRevStrategy(name="t", config=_default_cfg(ema_alpha=0))
    with pytest.raises(ValueError, match="ema_alpha"):
        MeanRevStrategy(name="t", config=_default_cfg(ema_alpha=1.5))
    with pytest.raises(ValueError, match="threshold_logit"):
        MeanRevStrategy(name="t", config=_default_cfg(threshold_logit=0))
    with pytest.raises(ValueError, match="trade_pct_of_cash"):
        MeanRevStrategy(name="t", config=_default_cfg(trade_pct_of_cash=2))
    with pytest.raises(ValueError, match="min_trade"):
        MeanRevStrategy(name="t", config=_default_cfg(min_trade=0))
    with pytest.raises(ValueError, match="size_scale_cap"):
        MeanRevStrategy(name="t", config=_default_cfg(size_scale_cap=0.5))


# ─── 2. setup ────────────────────────────────────────────────────

async def test_setup_bootstraps_holdings_and_cash(store):
    s = MeanRevStrategy(name="t", config=_default_cfg())
    ctx = await _make_ctx(
        store, holding_a=Decimal("30"), holding_b=Decimal("20"),
        cash=Decimal("400"),
    )
    await s.setup(ctx)
    assert s._holding[1] == Decimal("30")
    assert s._holding[2] == Decimal("20")
    assert s._cash == Decimal("400")
    assert s.market_id == 1
    assert s._outcome_ids == (1, 2)


async def test_setup_replays_ema_from_trades_history(store):
    """trades 表里有历史 SSE event → setup 应回放 EMA + event_count，省 warmup。

    防冷市场被 liveness 误杀后重启卡在 warmup_events=20 永远不出信号。
    """
    # 先在 trades 表种 25 笔历史成交（模拟上一轮 trader 的 SSE 记录）
    import json as _json
    for i in range(25):
        await store.log_trade(market_id=1, payload={"trade": {
            "id": 9000 + i, "type": "BUY", "outcome_id": 1,
            "username": "histuser", "shares": 1.0, "price": 0.5,
            "gross": 0.5, "fee": 0.0,
            "post_market_price": 0.55,
            # 关键：mp[0] = outcome A 的 post 价（递增模拟）
            "market_prices_post": [0.50 + i * 0.005, 0.50 - i * 0.005],
            "timestamp": f"2026-05-18T10:{i:02d}:00Z",
        }})

    s = MeanRevStrategy(name="t", config=_default_cfg(warmup_events=20))
    ctx = await _make_ctx(store, cash=Decimal("500"))
    await s.setup(ctx)

    # 25 笔回放完，event_count >= warmup_events → warmup 完成
    assert s._event_count == 25, f"应回放 25 笔, got {s._event_count}"
    assert s._ema_logit is not None, "EMA 应被初始化"
    # 不立即 warmup_done 检查，但下一笔 event 应该直接跑信号判断（不走 warmup return）


async def test_setup_no_history_no_replay(store):
    """trades 表空 → 回放 0 笔，event_count=0，老 warmup 行为不变。"""
    s = MeanRevStrategy(name="t", config=_default_cfg(warmup_events=20))
    ctx = await _make_ctx(store, cash=Decimal("500"))
    await s.setup(ctx)
    assert s._event_count == 0, "空 history 应跳过回放"
    assert s._ema_logit is None


async def test_setup_bad_history_rows_skipped(store):
    """单笔 history 损坏（JSON 坏 / 字段缺）不应让 setup 失败，跳过坏行即可。"""
    # 一笔坏的（手工写 SQL 绕过 log_trade 的合法性检查）
    await store._conn.execute(
        "INSERT INTO trades (trade_id, ts, ingest_ts, market_id, outcome_id, side, "
        " shares, price, gross, fee, username, post_market_price, market_prices_post_json) "
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (9000, "2026-05-18T10:00:00Z", "2026-05-18T10:00:00Z", 1, 1, "BUY",
         "1.0", "0.5", "0.5", "0", None, "0.5", "{not valid json"),
    )
    # 一笔正常的
    await store.log_trade(market_id=1, payload={"trade": {
        "id": 9001, "type": "BUY", "outcome_id": 1,
        "username": "u", "shares": 1.0, "price": 0.5,
        "gross": 0.5, "fee": 0.0,
        "post_market_price": 0.5,
        "market_prices_post": [0.5, 0.5],
        "timestamp": "2026-05-18T10:01:00Z",
    }})
    s = MeanRevStrategy(name="t", config=_default_cfg(warmup_events=20))
    ctx = await _make_ctx(store, cash=Decimal("500"))
    await s.setup(ctx)
    assert s._event_count == 1, "坏行应被 skip，只回放好的那一笔"


async def test_setup_rejects_non_binary_market(store):
    rest = MagicMock()
    rest.get_market = AsyncMock(return_value=MarketDetail(
        id=1, title="t", status="trading", liquidity_b=10000.0,
        outcomes=[
            OutcomeDetail(id=i, label=f"O{i}", total_shares=Decimal("0"),
                          current_price=Decimal("0.333"))
            for i in (1, 2, 3)
        ],
    ))
    rest.get_holdings = AsyncMock(return_value=[])
    rest.get_user_summary = AsyncMock(return_value=_summary(Decimal("500")))
    ctx = StrategyContext(
        rest=rest, broker=MagicMock(), store=store,
        logger=structlog.get_logger("test"),
        config={"max_slippage_bps": 300},
    )
    s = MeanRevStrategy(name="t", config=_default_cfg())
    with pytest.raises(ValueError, match="binary"):
        await s.setup(ctx)


# ─── 3. warmup ───────────────────────────────────────────────────

async def test_warmup_no_trades(store):
    s = MeanRevStrategy(name="t", config=_default_cfg(warmup_events=5))
    ctx = await _make_ctx(
        store, holding_a=Decimal("50"), cash=Decimal("500"),
    )
    await s.setup(ctx)
    # warmup=5：前 4 个 event 都应不下单（即使价格变化）
    for i in range(4):
        price = 0.5 if i < 2 else 0.8  # 后两笔大偏离
        await s.on_sse_event(_trade_event(seq=i, price_a=price))
    assert ctx.broker.buy.call_count == 0
    assert ctx.broker.sell.call_count == 0


# ─── 4. deadband ─────────────────────────────────────────────────

async def test_deadband_no_trade(store):
    s = MeanRevStrategy(name="t", config=_default_cfg(
        warmup_events=2, threshold_logit=0.5,   # 高阈值
    ))
    ctx = await _make_ctx(
        store, holding_a=Decimal("50"), cash=Decimal("500"),
    )
    await s.setup(ctx)
    # warmup
    await s.on_sse_event(_trade_event(seq=0, price_a=0.5))
    await s.on_sse_event(_trade_event(seq=1, price_a=0.5))
    # 价格 0.6 ≈ logit 0.405；EMA 跟踪后偏离不到 0.5 阈值
    await s.on_sse_event(_trade_event(seq=2, price_a=0.6))
    assert ctx.broker.buy.call_count == 0
    assert ctx.broker.sell.call_count == 0


# ─── 5. 信号 → 决策树 ────────────────────────────────────────────

async def test_overpriced_sells_a_when_holding(store):
    """A 偏高 + 有 A 持仓 → 卖 A"""
    s = MeanRevStrategy(name="t", config=_default_cfg(
        warmup_events=2, threshold_logit=0.1, ema_alpha=0.1,
    ))
    ctx = await _make_ctx(
        store, holding_a=Decimal("50"), holding_b=Decimal("0"),
        cash=Decimal("500"),
    )
    await s.setup(ctx)
    await s.on_sse_event(_trade_event(seq=0, price_a=0.5))
    await s.on_sse_event(_trade_event(seq=1, price_a=0.5))
    await s.on_sse_event(_trade_event(seq=2, price_a=0.7))   # A 偏高
    assert ctx.broker.sell.call_count == 1
    assert ctx.broker.buy.call_count == 0
    assert ctx.broker.sell.await_args.kwargs["outcome_id"] == 1


async def test_overpriced_buys_b_when_no_a(store):
    """A 偏高 + 不持 A → 买 B（synthetic short A）"""
    s = MeanRevStrategy(name="t", config=_default_cfg(
        warmup_events=2, threshold_logit=0.1, ema_alpha=0.1,
    ))
    ctx = await _make_ctx(
        store, holding_a=Decimal("0"), holding_b=Decimal("0"),
        cash=Decimal("500"),
    )
    await s.setup(ctx)
    await s.on_sse_event(_trade_event(seq=0, price_a=0.5))
    await s.on_sse_event(_trade_event(seq=1, price_a=0.5))
    await s.on_sse_event(_trade_event(seq=2, price_a=0.7))
    assert ctx.broker.sell.call_count == 0
    assert ctx.broker.buy.call_count == 1
    assert ctx.broker.buy.await_args.kwargs["outcome_id"] == 2


async def test_underpriced_sells_b_when_holding(store):
    """A 偏低 + 有 B 持仓 → 卖 B"""
    s = MeanRevStrategy(name="t", config=_default_cfg(
        warmup_events=2, threshold_logit=0.1, ema_alpha=0.1,
    ))
    ctx = await _make_ctx(
        store, holding_a=Decimal("0"), holding_b=Decimal("50"),
        cash=Decimal("500"),
    )
    await s.setup(ctx)
    await s.on_sse_event(_trade_event(seq=0, price_a=0.5))
    await s.on_sse_event(_trade_event(seq=1, price_a=0.5))
    await s.on_sse_event(_trade_event(seq=2, price_a=0.3))   # A 偏低
    assert ctx.broker.sell.call_count == 1
    assert ctx.broker.buy.call_count == 0
    assert ctx.broker.sell.await_args.kwargs["outcome_id"] == 2


async def test_underpriced_buys_a_when_no_b(store):
    """A 偏低 + 不持 B → 买 A"""
    s = MeanRevStrategy(name="t", config=_default_cfg(
        warmup_events=2, threshold_logit=0.1, ema_alpha=0.1,
    ))
    ctx = await _make_ctx(
        store, holding_a=Decimal("0"), holding_b=Decimal("0"),
        cash=Decimal("500"),
    )
    await s.setup(ctx)
    await s.on_sse_event(_trade_event(seq=0, price_a=0.5))
    await s.on_sse_event(_trade_event(seq=1, price_a=0.5))
    await s.on_sse_event(_trade_event(seq=2, price_a=0.3))
    assert ctx.broker.buy.call_count == 1
    assert ctx.broker.buy.await_args.kwargs["outcome_id"] == 1


# ─── 6. sizing ───────────────────────────────────────────────────

async def test_size_floor_to_int(store):
    """cash=500, pct=0.10 → target=50；A 价 0.3，50/0.3 = 166.6 → floor 166"""
    s = MeanRevStrategy(name="t", config=_default_cfg(
        warmup_events=2, threshold_logit=0.1, ema_alpha=0.1,
        trade_pct_of_cash=0.10, max_holding_per_side=1000,
    ))
    ctx = await _make_ctx(store, cash=Decimal("500"))
    await s.setup(ctx)
    await s.on_sse_event(_trade_event(seq=0, price_a=0.5))
    await s.on_sse_event(_trade_event(seq=1, price_a=0.5))
    await s.on_sse_event(_trade_event(seq=2, price_a=0.3))    # 买 A
    assert ctx.broker.buy.await_args.kwargs["shares"] == Decimal("166")


async def test_size_capped_by_max_holding(store):
    """max_holding=10, holding_a=5 → 剩余 cap=5；即便算出来 166 也只买 5"""
    s = MeanRevStrategy(name="t", config=_default_cfg(
        warmup_events=2, threshold_logit=0.1, ema_alpha=0.1,
        trade_pct_of_cash=0.10, max_holding_per_side=10,
    ))
    ctx = await _make_ctx(
        store, holding_a=Decimal("5"), holding_b=Decimal("0"),
        cash=Decimal("500"),
    )
    await s.setup(ctx)
    await s.on_sse_event(_trade_event(seq=0, price_a=0.5))
    await s.on_sse_event(_trade_event(seq=1, price_a=0.5))
    await s.on_sse_event(_trade_event(seq=2, price_a=0.3))    # 买 A
    assert ctx.broker.buy.await_args.kwargs["shares"] == Decimal("5")


async def test_size_scales_with_deviation(store):
    """|deviation| 是 threshold 的 2 倍 → target_amount 双倍。
    cash=500, pct=0.10 → base target=50, ×2 = 100；A 价 0.3，100/0.3 = 333.3 → floor 333"""
    s = MeanRevStrategy(name="t", config=_default_cfg(
        warmup_events=2, threshold_logit=0.5, ema_alpha=0.1,   # alpha 小让 EMA 几乎不动
        trade_pct_of_cash=0.10, size_scale_cap=3.0,
        max_holding_per_side=10000,
    ))
    ctx = await _make_ctx(store, cash=Decimal("500"))
    await s.setup(ctx)
    # 两笔 warmup at 0.5：EMA 初始化 = to_logit(0.5) = 0
    await s.on_sse_event(_trade_event(seq=0, price_a=0.5))
    await s.on_sse_event(_trade_event(seq=1, price_a=0.5))
    # 触发：to_logit(0.27) ≈ -0.995；EMA 几乎不动 ≈ -0.1
    # deviation ≈ -0.9；|deviation| / threshold(0.5) ≈ 1.8 → multiplier ≈ 1.8
    # target = 500 * 0.1 * 1.8 = 90；A 价 0.27，shares = 90/0.27 ≈ 333.3 → 333
    await s.on_sse_event(_trade_event(seq=2, price_a=0.27))
    shares = ctx.broker.buy.await_args.kwargs["shares"]
    # 允许小范围波动（EMA 那一点微动），但要远大于 base size 的 floor 185 (50/0.27)
    assert shares > Decimal("280"), f"expected size > 280 (scaled), got {shares}"
    assert shares < Decimal("400"), f"expected size < 400 (capped near 1.8x), got {shares}"


async def test_size_scale_capped(store):
    """|deviation| 远超 threshold → multiplier 被 size_scale_cap 限制。"""
    s = MeanRevStrategy(name="t", config=_default_cfg(
        warmup_events=2, threshold_logit=0.05, ema_alpha=0.01,  # 极小 alpha, EMA 几乎不动
        trade_pct_of_cash=0.10, size_scale_cap=2.0,             # cap 2
        max_holding_per_side=10000,
    ))
    ctx = await _make_ctx(store, cash=Decimal("500"))
    await s.setup(ctx)
    await s.on_sse_event(_trade_event(seq=0, price_a=0.5))
    await s.on_sse_event(_trade_event(seq=1, price_a=0.5))
    # 极端价格 0.05 → logit ≈ -2.94；deviation 远大于 threshold；should cap 到 2x
    # target = 500 * 0.1 * 2.0 = 100；A 价 0.05，shares = 100/0.05 = 2000
    await s.on_sse_event(_trade_event(seq=2, price_a=0.05))
    shares = ctx.broker.buy.await_args.kwargs["shares"]
    # 期望 ≈ 2000；如果没 cap 会是 5000+
    assert Decimal("1900") < shares <= Decimal("2100"), f"expected ~2000 (2x cap), got {shares}"


async def test_size_below_min_skips(store):
    """target_amount * 1/price < min_trade → skip + log"""
    s = MeanRevStrategy(name="t", config=_default_cfg(
        warmup_events=2, threshold_logit=0.1, ema_alpha=0.1,
        trade_pct_of_cash=0.0001, min_trade=1,
    ))
    ctx = await _make_ctx(store, cash=Decimal("10"))
    # target = 10 * 0.0001 = 0.001；除 price ≈ 0 股；floor → 0；< min_trade=1
    await s.setup(ctx)
    await s.on_sse_event(_trade_event(seq=0, price_a=0.5))
    await s.on_sse_event(_trade_event(seq=1, price_a=0.5))
    await s.on_sse_event(_trade_event(seq=2, price_a=0.3))
    assert ctx.broker.buy.call_count == 0
    assert ctx.broker.sell.call_count == 0
    rows = await store.recent_decisions(strategy="t", limit=10)
    assert any(r["reason"] == "size_below_min" for r in rows)


async def test_no_cash_skips(store):
    s = MeanRevStrategy(name="t", config=_default_cfg(
        warmup_events=2, threshold_logit=0.1, ema_alpha=0.1,
    ))
    ctx = await _make_ctx(store, cash=Decimal("0"))
    await s.setup(ctx)
    await s.on_sse_event(_trade_event(seq=0, price_a=0.5))
    await s.on_sse_event(_trade_event(seq=1, price_a=0.5))
    await s.on_sse_event(_trade_event(seq=2, price_a=0.3))
    assert ctx.broker.buy.call_count == 0
    rows = await store.recent_decisions(strategy="t", limit=10)
    assert any(r["reason"] == "no_cash" for r in rows)


# ─── 7. broker 异常处理 ──────────────────────────────────────────

async def test_risk_rejected_logs_skip_not_raises(store):
    s = MeanRevStrategy(name="t", config=_default_cfg(
        warmup_events=2, threshold_logit=0.1, ema_alpha=0.1,
    ))
    ctx = await _make_ctx(store, cash=Decimal("500"))
    ctx.broker.buy = AsyncMock(side_effect=RiskRejected("over cap"))
    await s.setup(ctx)
    await s.on_sse_event(_trade_event(seq=0, price_a=0.5))
    await s.on_sse_event(_trade_event(seq=1, price_a=0.5))
    await s.on_sse_event(_trade_event(seq=2, price_a=0.3))    # 想买 A 被拒
    rows = await store.recent_decisions(strategy="t", limit=10)
    assert any("buy_failed" in r["reason"] for r in rows)


async def test_holding_and_cash_updated_after_trade(store):
    s = MeanRevStrategy(name="t", config=_default_cfg(
        warmup_events=2, threshold_logit=0.1, ema_alpha=0.1,
    ))
    ctx = await _make_ctx(
        store, cash=Decimal("500"),
        buy_shares_resp=Decimal("100"),
    )
    # broker mock 返回 new_cash = 495
    await s.setup(ctx)
    await s.on_sse_event(_trade_event(seq=0, price_a=0.5))
    await s.on_sse_event(_trade_event(seq=1, price_a=0.5))
    await s.on_sse_event(_trade_event(seq=2, price_a=0.3))
    assert s._holding[1] == Decimal("100")   # 买入累加
    assert s._cash == Decimal("495")          # broker 返回的 new_cash


# ─── 8. 集成：SseSubscriber dispatch ──────────────────────────────

async def test_subscriber_dispatches_event(store):
    """真 SseSubscriber + mock SseClient → strategy 收到事件并下单"""
    s = MeanRevStrategy(name="t", config=_default_cfg(
        warmup_events=1, threshold_logit=0.1, ema_alpha=0.1,
    ))
    ctx = await _make_ctx(
        store, holding_a=Decimal("50"), cash=Decimal("500"),
    )
    await s.setup(ctx)

    events = [
        _trade_event(seq=0, price_a=0.5),   # warmup (count==1, < warmup=1 不成立 → 实际过)
        _trade_event(seq=1, price_a=0.7),   # 触发卖 A
    ]
    # warmup=1 表示 count >= 1 就开始下单；第 0 个 event count=1 已达
    # 价格还是 0.5 deviation=0，所以不会下单；第 1 个 event 才触发
    # 但 EMA 已经被第 0 个事件初始化为 0；第 1 个事件 price=0.7 触发

    sse = MagicMock()

    async def fake_subscribe(market_id):
        for ev in events:
            yield ev
        # 模拟 SSE 流结束 → run() 退出
    sse.subscribe = fake_subscribe

    rest = MagicMock()
    rest.get_recent_trades = AsyncMock(return_value=[])
    sub = SseSubscriber(
        rest=rest, store=store, sse_client=sse,
        strategies=[s], market_ids={1},
        logger=structlog.get_logger("test"),
    )
    try:
        await asyncio.wait_for(sub.run(), timeout=2.0)
    except asyncio.TimeoutError:
        pass

    # subscriber 应该把事件 dispatch 给策略，策略卖 A
    assert ctx.broker.sell.call_count >= 1
