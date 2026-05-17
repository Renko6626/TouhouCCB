import time
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from thccb_quant.broker.live import LiveBroker
from thccb_quant.broker.dryrun import DryRunBroker
from thccb_quant.broker.risk import RiskConfig, RiskGuard
from thccb_quant.client.auth import TokenManager
from thccb_quant.client.rest import RestClient
from thccb_quant.errors import BusinessError, RiskRejected
from thccb_quant.state.store import Store


def _jwt(exp=3600):
    import base64, json
    p = {"exp": int(time.time()) + exp}
    return (
        base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
        + "."
        + base64.urlsafe_b64encode(json.dumps(p).encode()).rstrip(b"=").decode()
        + ".sig"
    )


@pytest.fixture
async def deps(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    store = await Store.open(state_dir / "test.db")
    cfg = RiskConfig(
        single_order_cap_cny=Decimal("50"),
        daily_loss_cap_cny=Decimal("100"),
        daily_turnover_cap_cny=Decimal("2000"),
        max_slippage_bps=300,
        min_seconds_between_orders=0,  # 测试不要冷却干扰
    )
    risk = RiskGuard(cfg, state_dir)
    async with httpx.AsyncClient(base_url="http://x") as raw:
        mgr = TokenManager(
            base_url="http://x",
            access_token=_jwt(),
            refresh_token=_jwt(86400),
            env_path=env,
            raw_client=raw,
        )
        async with httpx.AsyncClient(base_url="http://x") as client:
            rest = RestClient(client=client, token_manager=mgr, rate_limit_per_sec=100)
            yield rest, risk, store
    await store.close()


@respx.mock
async def test_live_buy_success_logs_order(deps):
    rest, risk, store = deps
    respx.post("http://x/api/v1/market/quote").mock(
        return_value=httpx.Response(200, json={
            "outcome_id": 1, "side": "buy", "shares": "2.5",
            "avg_price": "0.42", "gross": "1.05", "fee": "0", "net": "1.05",
            "after_prices": [],
        })
    )
    respx.post("http://x/api/v1/market/buy").mock(
        return_value=httpx.Response(200, json={
            "shares": 2.5, "cost": 1.05, "new_cash": 498.95, "message": "ok",
        })
    )
    broker = LiveBroker(rest=rest, risk=risk, store=store)
    resp = await broker.buy(
        strategy="g", outcome_id=1, shares=Decimal("2.5"), max_slippage_bps=200
    )
    assert resp.cost == Decimal("1.05")
    orders = await store.recent_orders(strategy="g", limit=10)
    assert len(orders) == 1
    assert orders[0]["status"] == "success"


@respx.mock
async def test_live_buy_400_logs_failed(deps):
    rest, risk, store = deps
    respx.post("http://x/api/v1/market/quote").mock(
        return_value=httpx.Response(200, json={
            "outcome_id": 1, "side": "buy", "shares": "2.5",
            "avg_price": "0.42", "gross": "1.05", "fee": "0", "net": "1.05",
            "after_prices": [],
        })
    )
    respx.post("http://x/api/v1/market/buy").mock(
        return_value=httpx.Response(400, json={"detail": "余额不足"})
    )
    broker = LiveBroker(rest=rest, risk=risk, store=store)
    with pytest.raises(BusinessError):
        await broker.buy(strategy="g", outcome_id=1, shares=Decimal("2.5"),
                         max_slippage_bps=200)
    orders = await store.recent_orders(strategy="g", limit=10)
    assert len(orders) == 1
    assert orders[0]["status"] == "failed"


@respx.mock
async def test_quote_fail_does_not_call_buy(deps):
    rest, risk, store = deps
    quote_route = respx.post("http://x/api/v1/market/quote").mock(
        return_value=httpx.Response(500)
    )
    buy_route = respx.post("http://x/api/v1/market/buy")
    broker = LiveBroker(rest=rest, risk=risk, store=store)
    with pytest.raises(Exception):
        await broker.buy(strategy="g", outcome_id=1, shares=Decimal("2.5"),
                         max_slippage_bps=200)
    assert quote_route.called
    assert not buy_route.called


@respx.mock
async def test_idempotent_duplicate_within_5s(deps):
    rest, risk, store = deps
    respx.post("http://x/api/v1/market/quote").mock(
        return_value=httpx.Response(200, json={
            "outcome_id": 1, "side": "buy", "shares": "2.5",
            "avg_price": "0.42", "gross": "1.05", "fee": "0", "net": "1.05",
            "after_prices": [],
        })
    )
    respx.post("http://x/api/v1/market/buy").mock(
        return_value=httpx.Response(200, json={
            "shares": 2.5, "cost": 1.05, "new_cash": 498.95, "message": "ok",
        })
    )
    broker = LiveBroker(rest=rest, risk=risk, store=store)
    await broker.buy(strategy="g", outcome_id=1, shares=Decimal("2.5"),
                     max_slippage_bps=200)
    with pytest.raises(RiskRejected, match="duplicate"):
        await broker.buy(strategy="g", outcome_id=1, shares=Decimal("2.5"),
                         max_slippage_bps=200)


@respx.mock
async def test_dryrun_does_not_call_buy(deps):
    rest, risk, store = deps
    respx.post("http://x/api/v1/market/quote").mock(
        return_value=httpx.Response(200, json={
            "outcome_id": 1, "side": "buy", "shares": "2.5",
            "avg_price": "0.42", "gross": "1.05", "fee": "0", "net": "1.05",
            "after_prices": [],
        })
    )
    buy_route = respx.post("http://x/api/v1/market/buy")
    broker = DryRunBroker(rest=rest, risk=risk, store=store)
    resp = await broker.buy(strategy="g", outcome_id=1, shares=Decimal("2.5"),
                            max_slippage_bps=200)
    assert resp.cost == Decimal("1.05")
    assert not buy_route.called
    orders = await store.recent_orders(strategy="g", limit=10)
    assert orders[0]["status"] == "dryrun"
