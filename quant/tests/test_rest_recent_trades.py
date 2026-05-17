import time
from decimal import Decimal
from pathlib import Path
import httpx
import pytest
import respx
from thccb_quant.client.auth import TokenManager
from thccb_quant.client.rest import RestClient


def _jwt(exp=3600):
    import base64, json
    p = {"exp": int(time.time()) + exp}
    return (
        base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
        + "." + base64.urlsafe_b64encode(json.dumps(p).encode()).rstrip(b"=").decode()
        + ".sig"
    )


@respx.mock
async def test_get_recent_trades_parses(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("")
    respx.get("http://x/api/v1/market/recent-trades").mock(
        return_value=httpx.Response(200, json=[
            {"id": 10, "outcome_id": 1, "type": "BUY", "shares": "2.5",
             "price": "0.42", "username": "alice",
             "timestamp": "2026-05-17T07:00:00Z",
             "market_id": 1, "market_title": "M", "outcome_label": "yes"},
        ])
    )
    async with httpx.AsyncClient(base_url="http://x") as raw, \
               httpx.AsyncClient(base_url="http://x") as cli:
        mgr = TokenManager(base_url="http://x", access_token=_jwt(),
                           refresh_token=_jwt(86400), env_path=env, raw_client=raw)
        rest = RestClient(client=cli, token_manager=mgr, rate_limit_per_sec=100)
        rs = await rest.get_recent_trades(limit=100)
    assert len(rs) == 1
    assert rs[0].id == 10
    assert rs[0].username == "alice"
    assert rs[0].market_id == 1
