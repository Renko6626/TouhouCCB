from decimal import Decimal
from pathlib import Path
import pytest
from thccb_quant.state.store import Store


@pytest.fixture
async def store(tmp_path: Path):
    s = await Store.open(tmp_path / "t.db")
    yield s
    await s.close()


async def test_log_trade_inserts_full_row(store: Store):
    payload = {
        "trade": {
            "id": 1001, "type": "BUY", "outcome_id": 5,
            "username": "alice", "shares": 2.5, "price": 0.42,
            "gross": 1.05, "fee": 0.0, "post_market_price": 0.43,
            "market_prices_post": [0.43, 0.57], "timestamp": "2026-05-17T07:00:00Z",
        }
    }
    is_new = await store.log_trade(market_id=1, payload=payload)
    assert is_new is True
    rows = await store.recent_trades_observed(market_id=1, limit=10)
    assert len(rows) == 1
    assert rows[0]["trade_id"] == 1001
    assert rows[0]["username"] == "alice"
    assert rows[0]["market_prices_post_json"] == "[0.43, 0.57]"


async def test_log_trade_dedup_by_trade_id(store: Store):
    payload = {"trade": {
        "id": 2001, "type": "BUY", "outcome_id": 1, "username": "x",
        "shares": 1.0, "price": 0.5, "gross": 0.5, "fee": 0.0,
        "post_market_price": 0.5, "market_prices_post": [0.5, 0.5],
        "timestamp": "2026-05-17T07:00:00Z",
    }}
    first = await store.log_trade(market_id=1, payload=payload)
    second = await store.log_trade(market_id=1, payload=payload)  # INSERT OR IGNORE
    assert first is True, "第一次 insert 应返 True"
    assert second is False, "第二次（duplicate）应返 False；调用方据此 skip dispatch"
    rows = await store.recent_trades_observed(market_id=1, limit=10)
    assert len(rows) == 1


async def test_bulk_insert_partial_trades_idempotent(store: Store):
    items = [
        {"id": 100, "outcome_id": 1, "type": "BUY", "shares": "1.0",
         "price": "0.5", "username": "u1", "timestamp": "2026-05-17T07:00:00Z",
         "market_id": 1, "market_title": "M", "outcome_label": "yes"},
        {"id": 101, "outcome_id": 1, "type": "SELL", "shares": "2.0",
         "price": "0.6", "username": "u2", "timestamp": "2026-05-17T07:01:00Z",
         "market_id": 1, "market_title": "M", "outcome_label": "yes"},
    ]
    inserted = await store.bulk_insert_partial_trades(items)
    assert inserted == 2
    inserted2 = await store.bulk_insert_partial_trades(items)
    assert inserted2 == 0
