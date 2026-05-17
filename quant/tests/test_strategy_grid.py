from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

from thccb_quant.strategy.grid import GridStrategy
from thccb_quant.strategy.base import StrategyContext
from thccb_quant.client.rest import (
    MarketDetail, OrderResponse, OutcomeDetail, QuoteResponse,
)
from thccb_quant.state.store import Store


@pytest.fixture
async def store(tmp_path: Path):
    s = await Store.open(tmp_path / "test.db")
    yield s
    await s.close()


def _market(price: float) -> MarketDetail:
    return MarketDetail(
        id=1, title="t", status="trading", liquidity_b=100.0,
        outcomes=[
            OutcomeDetail(id=1, label="yes", total_shares=Decimal("10"),
                          current_price=Decimal(str(price))),
            OutcomeDetail(id=2, label="no", total_shares=Decimal("5"),
                          current_price=Decimal(str(1 - price))),
        ],
    )


async def _make_ctx(store: Store, current_price: float) -> StrategyContext:
    rest = MagicMock()
    rest.get_market = AsyncMock(return_value=_market(current_price))
    rest.quote = AsyncMock(return_value=QuoteResponse(
        outcome_id=1, side="buy", shares=Decimal("2"),
        avg_price=Decimal(str(current_price)),
        gross=Decimal(str(current_price * 2)), fee=Decimal("0"),
        net=Decimal(str(current_price * 2)),
    ))
    broker = MagicMock()
    broker.buy = AsyncMock(return_value=OrderResponse(
        shares=Decimal("2"), cost=Decimal(str(current_price * 2)),
        new_cash=Decimal("500"),
    ))
    broker.sell = AsyncMock(return_value=OrderResponse(
        shares=Decimal("2"), cost=Decimal(str(-current_price * 2)),
        new_cash=Decimal("510"),
    ))
    return StrategyContext(
        rest=rest, broker=broker, store=store,
        logger=structlog.get_logger("test"), config={},
    )


def _cfg():
    return {
        "market_id": 1, "outcome_id": 1,
        "price_low": 0.30, "price_high": 0.60,
        "grid_count": 4, "shares_per_grid": 2.0,
    }


async def test_grid_buys_when_below_grid_point(store: Store):
    s = GridStrategy(name="g", config=_cfg())
    ctx = await _make_ctx(store, current_price=0.32)
    await s.setup(ctx)
    await s.tick()
    assert ctx.broker.buy.call_count >= 1


async def test_grid_no_action_above_high(store: Store):
    s = GridStrategy(name="g", config=_cfg())
    ctx = await _make_ctx(store, current_price=0.70)
    await s.setup(ctx)
    await s.tick()
    assert ctx.broker.buy.call_count == 0


async def test_grid_no_double_buy_same_grid(store: Store):
    s = GridStrategy(name="g", config=_cfg())
    ctx = await _make_ctx(store, current_price=0.32)
    await s.setup(ctx)
    await s.tick()
    first = ctx.broker.buy.call_count
    await s.tick()  # 价格没变，同一格点不应再触发
    assert ctx.broker.buy.call_count == first


async def test_grid_sells_after_rebound(store: Store):
    s = GridStrategy(name="g", config=_cfg())
    ctx = await _make_ctx(store, current_price=0.32)
    await s.setup(ctx)
    await s.tick()
    # 模拟价格回升到 0.55，应触发卖出
    ctx.rest.get_market = AsyncMock(return_value=_market(0.55))
    await s.tick()
    assert ctx.broker.sell.call_count >= 1


async def test_grid_setup_sets_market_id(store: Store):
    s = GridStrategy(name="g", config=_cfg())
    ctx = await _make_ctx(store, current_price=0.40)
    await s.setup(ctx)
    assert s.market_id == 1
