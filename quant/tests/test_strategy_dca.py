import time
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

from thccb_quant.strategy.dca import DcaStrategy
from thccb_quant.strategy.base import StrategyContext
from thccb_quant.client.rest import OrderResponse, QuoteResponse
from thccb_quant.state.store import Store


@pytest.fixture
async def store(tmp_path: Path):
    s = await Store.open(tmp_path / "test.db")
    yield s
    await s.close()


async def _make_ctx(store: Store, broker_buy_resp=None) -> StrategyContext:
    rest = MagicMock()
    rest.quote = AsyncMock(return_value=QuoteResponse(
        outcome_id=1, side="buy", shares=Decimal("10"),
        avg_price=Decimal("0.5"), gross=Decimal("5"), fee=Decimal("0"),
        net=Decimal("5"),
    ))
    broker = MagicMock()
    broker.buy = AsyncMock(return_value=broker_buy_resp or OrderResponse(
        shares=Decimal("10"), cost=Decimal("5"), new_cash=Decimal("495"),
    ))
    return StrategyContext(
        rest=rest, broker=broker, store=store,
        logger=structlog.get_logger("test"), config={},
    )


async def test_dca_buys_at_interval(store: Store):
    cfg = {
        "market_id": 1,
        "outcome_id": 1, "cny_per_buy": 5.0,
        "interval_hours": 6, "total_budget_cny": 200,
    }
    s = DcaStrategy(name="d", config=cfg)
    ctx = await _make_ctx(store)
    await s.setup(ctx)
    await s.tick()
    assert ctx.broker.buy.call_count == 1


async def test_dca_respects_interval(store: Store):
    cfg = {
        "market_id": 1,
        "outcome_id": 1, "cny_per_buy": 5.0,
        "interval_hours": 6, "total_budget_cny": 200,
    }
    s = DcaStrategy(name="d", config=cfg)
    ctx = await _make_ctx(store)
    await s.setup(ctx)
    await s.tick()
    await s.tick()  # 立刻第二次，应该跳过
    assert ctx.broker.buy.call_count == 1


async def test_dca_respects_total_budget(store: Store):
    cfg = {
        "market_id": 1,
        "outcome_id": 1, "cny_per_buy": 5.0,
        "interval_hours": 0,  # 间隔为 0，纯靠预算限制
        "total_budget_cny": 10,
    }
    s = DcaStrategy(name="d", config=cfg)
    ctx = await _make_ctx(store)
    await s.setup(ctx)
    await s.tick()
    await s.tick()
    await s.tick()  # 第三次应该被预算挡住
    assert ctx.broker.buy.call_count == 2


async def test_dca_setup_sets_market_id(store: Store):
    cfg = {"market_id": 7, "outcome_id": 1, "cny_per_buy": 5.0,
           "interval_hours": 6, "total_budget_cny": 200}
    s = DcaStrategy(name="d", config=cfg)
    ctx = await _make_ctx(store)
    await s.setup(ctx)
    assert s.market_id == 7


async def test_dca_missing_market_id_raises_keyerror(store: Store):
    """DCA config 缺 market_id 应在 __init__ 抛 KeyError，trader 应能 catch + 友好提示。"""
    cfg = {"outcome_id": 1, "cny_per_buy": 5.0,
           "interval_hours": 6, "total_budget_cny": 200}  # 故意没 market_id
    import pytest
    with pytest.raises(KeyError, match="market_id"):
        DcaStrategy(name="d", config=cfg)
