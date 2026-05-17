import asyncio
import time
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from thccb_quant.client.auth import TokenManager
from thccb_quant.client.rest import RestClient
from thccb_quant.errors import BusinessError, TransientError


def _jwt(exp_secs: int = 3600):
    import base64, json
    payload = {"exp": int(time.time()) + exp_secs}
    return (
        base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
        + "."
        + base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        + ".sig"
    )


@pytest.fixture
async def rest_pair(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("")
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
            yield rest


@respx.mock
async def test_quote_returns_parsed(rest_pair: RestClient):
    respx.post("http://x/api/v1/market/quote").mock(
        return_value=httpx.Response(
            200,
            json={
                "outcome_id": 1, "side": "buy", "shares": "2.5",
                "avg_price": "0.42", "gross": "1.05", "fee": "0", "net": "1.05",
                "after_prices": [],
            },
        )
    )
    q = await rest_pair.quote(outcome_id=1, shares=Decimal("2.5"), side="buy")
    assert q.gross == Decimal("1.05")


@respx.mock
async def test_buy_4xx_raises_business(rest_pair: RestClient):
    respx.post("http://x/api/v1/market/buy").mock(
        return_value=httpx.Response(400, json={"detail": "现金不足"})
    )
    with pytest.raises(BusinessError) as ei:
        await rest_pair.buy(outcome_id=1, shares=Decimal("2.5"), max_slippage_bps=300)
    assert ei.value.status == 400


@respx.mock
async def test_5xx_retries_then_transient(rest_pair: RestClient):
    route = respx.get("http://x/api/v1/market/1").mock(
        return_value=httpx.Response(503)
    )
    with pytest.raises(TransientError):
        await rest_pair.get_market(1)
    assert route.call_count >= 2  # 重试过


@respx.mock
async def test_rate_limit_throttles(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("")
    respx.get("http://x/api/v1/market/1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 1, "title": "t", "status": "trading", "liquidity_b": 100.0,
                "outcomes": [], "last_trade_at": None,
            },
        )
    )
    async with httpx.AsyncClient(base_url="http://x") as raw:
        mgr = TokenManager(
            base_url="http://x",
            access_token=_jwt(),
            refresh_token=_jwt(86400),
            env_path=env,
            raw_client=raw,
        )
        async with httpx.AsyncClient(base_url="http://x") as client:
            rest = RestClient(client=client, token_manager=mgr, rate_limit_per_sec=4)
            start = time.monotonic()
            await asyncio.gather(*[rest.get_market(1) for _ in range(8)])
            elapsed = time.monotonic() - start
    # 8 个请求 / 4 r/s 至少要 ~1s
    assert elapsed > 0.9
