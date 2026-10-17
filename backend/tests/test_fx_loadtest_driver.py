"""A slow service must show missed client cadence in the load-test report."""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("fx_http", Path(__file__).resolve().parents[2] / "loadtest/fx_http.py")
fx_http = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fx_http)


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
