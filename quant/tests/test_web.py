"""thccb_quant.web FastAPI 端点测试。

不启 uvicorn server，直接用 FastAPI TestClient 打 ASGI；省 1 个端口、跑得快。
"""
from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from thccb_quant.runtime import RUNTIME, Runtime
from thccb_quant.state.store import Store
from thccb_quant.strategy.meanrev import MeanRevStrategy
from thccb_quant.web import make_app


@pytest.fixture
async def store(tmp_path: Path):
    s = await Store.open(tmp_path / "t.db")
    yield s
    await s.close()


@pytest.fixture
def client():
    """Reset RUNTIME state 然后给一个 TestClient。"""
    # Reset RUNTIME；不同测试间互不污染
    RUNTIME.started_at = time.monotonic()
    RUNTIME.started_at_wall = time.time()
    RUNTIME.dry_run = False
    RUNTIME.base_url = ""
    RUNTIME.strategies = []
    RUNTIME.subscriber = None
    RUNTIME.store = None
    RUNTIME.config = {}
    return TestClient(make_app())


# ─── /api/status ──────────────────────────────────────────

def test_status_empty(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    j = r.json()
    assert "uptime_sec" in j and j["uptime_sec"] >= 0
    assert j["strategies_count"] == 0
    assert j["sse"]["active"] is False
    assert j["sse"]["idle_sec"] is None


def test_status_with_subscriber(client):
    fake_sub = MagicMock()
    fake_sub.last_event_handled_ts = time.monotonic() - 5.0
    fake_sub._market_ids = {1, 3}
    RUNTIME.subscriber = fake_sub
    RUNTIME.base_url = "http://localhost:8004"
    RUNTIME.dry_run = True

    r = client.get("/api/status")
    j = r.json()
    assert j["base_url"] == "http://localhost:8004"
    assert j["dry_run"] is True
    assert j["sse"]["active"] is True
    assert j["sse"]["idle_sec"] is not None and 4 < j["sse"]["idle_sec"] < 7
    assert j["sse"]["subscribed_markets"] == [1, 3]


# ─── /api/strategies ──────────────────────────────────────

def _meanrev_cfg(**ov):
    cfg = dict(
        market_id=1, ema_alpha=0.1, threshold_logit=0.08, warmup_events=20,
        trade_pct_of_cash=0.05, size_scale_cap=3.0, min_trade=10,
        max_holding_per_side=200,
    )
    cfg.update(ov)
    return cfg


def test_strategies_returns_snapshots(client):
    s = MeanRevStrategy(name="meanrev_1", config=_meanrev_cfg())
    RUNTIME.strategies = [s]
    r = client.get("/api/strategies")
    j = r.json()
    assert len(j) == 1
    snap = j[0]
    assert snap["name"] == "meanrev_1"
    assert snap["type"] == "meanrev"
    assert snap["threshold_logit"] == 0.08
    assert snap["warmup_done"] is False  # 还没收事件
    assert "holding" in snap and "cash" in snap


def test_strategies_handles_snapshot_exception(client):
    """单个策略 snapshot() 抛错不该把整个 endpoint 拉死。"""
    broken = MagicMock()
    broken.name = "broken"
    broken.snapshot = MagicMock(side_effect=RuntimeError("boom"))
    good = MeanRevStrategy(name="good", config=_meanrev_cfg())
    RUNTIME.strategies = [broken, good]
    r = client.get("/api/strategies")
    j = r.json()
    assert len(j) == 2
    assert j[0]["name"] == "broken" and "error" in j[0]
    assert j[1]["name"] == "good"


# ─── /api/trades + orders + decisions ─────────────────────

@pytest.mark.asyncio
async def test_trades_endpoint(store, tmp_path):
    RUNTIME.store = store
    await store.log_trade(market_id=1, payload={
        "trade": {
            "id": 100, "outcome_id": 1, "type": "BUY",
            "shares": 5, "price": 0.5, "gross": 2.5, "fee": 0,
            "username": "alice", "post_market_price": 0.51,
            "market_prices_post": [0.51, 0.49],
            "timestamp": "2026-05-18T10:00:00Z",
        }
    })
    with TestClient(make_app()) as c:
        r = c.get("/api/trades?limit=10")
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 1
        assert rows[0]["market_id"] == 1
    RUNTIME.store = None


@pytest.mark.asyncio
async def test_orders_endpoint(store):
    RUNTIME.store = store
    await store.log_order(
        strategy="t", outcome_id=1, side="buy",
        shares=Decimal("10"), price=Decimal("0.5"), cost=Decimal("5"),
        status="success",
    )
    with TestClient(make_app()) as c:
        r = c.get("/api/orders?strategy=t")
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 1
        assert rows[0]["strategy"] == "t"
    RUNTIME.store = None


@pytest.mark.asyncio
async def test_decisions_endpoint(store):
    RUNTIME.store = store
    await store.log_decision(
        strategy="t", outcome_id=1, action="skip",
        reason="warmup", snapshot={"x": 1},
    )
    with TestClient(make_app()) as c:
        r = c.get("/api/decisions?strategy=t")
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 1
        assert rows[0]["action"] == "skip"
        assert rows[0]["reason"] == "warmup"
    RUNTIME.store = None


def test_endpoints_without_store(client):
    """RUNTIME.store=None 时不应 500，返空 list 即可（trader 启动早期可能这样）。"""
    assert client.get("/api/trades").json() == []
    assert client.get("/api/orders").json() == []
    assert client.get("/api/decisions").json() == []


# ─── / (HTML page) ────────────────────────────────────────

def test_index_returns_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    assert "thccb-quant monitor" in r.text
    assert "/api/status" in r.text  # JS 里会调
