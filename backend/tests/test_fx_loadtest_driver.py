"""A slow service must show missed client cadence in the load-test report."""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("fx_http", Path(__file__).resolve().parents[2] / "loadtest/fx_http.py")
fx_http = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fx_http)


def test_newer_sse_stamp_already_covers_version_check():
    current = {"epoch": "a", "revision": "10", "input_hash": "new"}
    assert fx_http.covers_stamp(current, {"epoch": "a", "revision": "9", "input_hash": "old"})
    assert not fx_http.covers_stamp(current, {"epoch": "a", "revision": "10", "input_hash": "different"})
    assert not fx_http.covers_stamp(current, {"epoch": "b", "revision": "9", "input_hash": "old"})


@pytest.mark.asyncio
async def test_slow_trade_reports_missed_schedule_without_parallel_requests(tmp_path, monkeypatch):
    metrics = fx_http.Metrics(tmp_path / "report")
    runner = fx_http.Runner(SimpleNamespace(min_interval=.01, max_interval=.01, mode="full"),
                            {"pair_id": 1}, metrics, None)
    active, maximum = 0, 0

    async def slow_service(user, *, force_buy=False):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(.06)
        active -= 1

    monkeypatch.setattr(runner, "trade", slow_service)
    try:
        await runner.stage("slow", [{"uid": 1}], .25)
        assert maximum == 1
        assert max(metrics.slot_lag["slow"]) >= 30  # 60ms service cannot meet a 10ms cadence.
    finally:
        metrics.samples.close()


@pytest.mark.asyncio
async def test_driver_finance_cross_and_cover_use_real_routes_and_refresh(tmp_path):
    import time
    from decimal import Decimal
    from datetime import datetime, timezone
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.core.users import create_access_token
    from app.core.database import async_session_maker
    from app.models.base import User
    from app.models.fx import FxShortPosition, FxWallet
    from sqlalchemy import select
    from tests.test_fx_short_trading import _seed

    uid, pid = await _seed(cash='100000')
    user = {"uid": uid, "token": create_access_token(uid)}
    args = SimpleNamespace(amount="10")
    metrics = fx_http.Metrics(tmp_path / "report")
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        runner = fx_http.Runner(args, {"pair_id": pid, "users": [user]}, metrics, client)
        boundary = int(time.time()) // 3600 * 3600
        runner.envelopes[uid] = {"price": "1", "history_version": "test", "history_ready": True,
            "history_tail_at": datetime.now(timezone.utc).isoformat(), "history_tail_through_event_id": 0,
            "history_tail": {"1m": {"t0": boundary}}}
        runner.chart_boundaries[uid] = boundary
        runner.roles[uid] = "finance"
        try:
            assert await runner.trade(user)
            async with async_session_maker() as db:
                assert (await db.get(User, uid)).debt > 0
            assert await runner.trade(user)
            runner.roles[uid] = "short"
            runner.cross_users.add(uid)
            for _ in range(3):
                assert await runner.trade(user)
            assert all(metrics.coverage[name] for name in ("financed_buy", "sell_repay", "cross_zero", "short_cover"))
            assert metrics.counts[("preflight", "my_trades_after_trade")]["ok"] == 5
            async with async_session_maker() as db:
                wallet = (await db.execute(select(FxWallet).where(FxWallet.user_id == uid))).scalar_one()
                short = (await db.execute(select(FxShortPosition).where(FxShortPosition.user_id == uid))).scalar_one()
                assert wallet.foreign_amount == 0
                assert short.principal_foreign + short.interest_foreign == 0
                assert (await db.get(User, uid)).debt == 0
        finally:
            metrics.samples.close()
