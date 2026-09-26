"""离线活动验证器必须守住真实资金/份额语义，且能够复现。"""
import pytest


@pytest.mark.asyncio
async def test_simulation_reuses_real_engine_without_creating_money():
    from scripts.simulate_pve_event import simulate
    report = await simulate(mode="event", hours=0.2, depths=[10000], outcomes=[2], seed=19)
    assert report["trades"] > 0
    assert report["min_cash"] >= 0
    assert abs(report["cash_remaining"] + report["market_cost_increase"]
               + report["fees_paid"] - 25000) < 0.02
    assert report["holdings_match_market"] is True


@pytest.mark.asyncio
async def test_simulation_same_seed_produces_same_metrics():
    from scripts.simulate_pve_event import simulate
    first = await simulate(mode="event", hours=0.1, depths=[3000, 10000, 30000],
                           outcomes=[2, 8, 20], seed=7)
    second = await simulate(mode="event", hours=0.1, depths=[3000, 10000, 30000],
                            outcomes=[2, 8, 20], seed=7)
    assert first == second


def test_minute_volatility_uses_log_returns_and_price_points_separately():
    from scripts.simulate_pve_event import minute_volatility
    result = minute_volatility([0.5, 0.55, 0.5])
    assert result["change_std_pp"] == pytest.approx(5.0)
    assert result["log_return_std_pct"] == pytest.approx(9.531018, abs=1e-5)
    assert minute_volatility([0.5, 0.5, 0.5]) == {"change_std_pp": 0, "log_return_std_pct": 0}
