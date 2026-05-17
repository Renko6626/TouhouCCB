from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from thccb_quant.state.store import Store


@pytest.fixture
async def store(tmp_path: Path):
    db_path = tmp_path / "test.db"
    s = await Store.open(db_path)
    yield s
    await s.close()


async def test_log_order_success(store: Store):
    await store.log_order(
        strategy="grid_x",
        outcome_id=1,
        side="buy",
        shares=Decimal("2.5"),
        price=Decimal("0.42"),
        cost=Decimal("1.05"),
        status="success",
    )
    rows = await store.recent_orders(strategy="grid_x", limit=10)
    assert len(rows) == 1
    assert rows[0]["status"] == "success"
    assert rows[0]["shares"] == "2.5"


async def test_has_recent_duplicate_window(store: Store):
    await store.log_order(
        strategy="g", outcome_id=1, side="buy",
        shares=Decimal("2.5"), price=Decimal("0.42"),
        cost=Decimal("1.05"), status="success",
    )
    assert await store.has_recent_duplicate(
        "g", 1, "buy", Decimal("2.5"), within_sec=5, statuses=("success", "dryrun")
    )
    # failed 单不算
    assert not await store.has_recent_duplicate(
        "g", 1, "buy", Decimal("3.0"), within_sec=5, statuses=("success", "dryrun")
    )


async def test_daily_stats_accumulate(store: Store):
    today = datetime.now(timezone.utc).date().isoformat()
    await store.add_turnover(Decimal("10.5"))
    await store.add_turnover(Decimal("4.5"))
    stats = await store.get_daily_stats(today)
    assert Decimal(stats["gross_turnover"]) == Decimal("15.0")


async def test_log_decision(store: Store):
    await store.log_decision(
        strategy="g", outcome_id=1, action="skip", reason="below threshold",
        snapshot={"price": 0.42},
    )
    rows = await store.recent_decisions(strategy="g", limit=10)
    assert len(rows) == 1
    assert rows[0]["action"] == "skip"


async def test_daily_stats_decimal_precision_preserved(store: Store):
    """REAL 算术会让 0.1+0.2 = 0.30000000000000004，应使用 Decimal 算术保精度。"""
    await store.add_turnover(Decimal("0.1"))
    await store.add_turnover(Decimal("0.2"))
    today = datetime.now(timezone.utc).date().isoformat()
    stats = await store.get_daily_stats(today)
    assert Decimal(stats["gross_turnover"]) == Decimal("0.3")

    await store.add_pnl(Decimal("-0.123456"))
    await store.add_pnl(Decimal("-0.234567"))
    stats = await store.get_daily_stats(today)
    assert Decimal(stats["net_pnl"]) == Decimal("-0.358023")
