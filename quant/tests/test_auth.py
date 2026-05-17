import asyncio
import base64
import json
import time
from pathlib import Path

import httpx
import pytest
import respx

from thccb_quant.client.auth import TokenManager
from thccb_quant.errors import FatalAuthError


def _mk_jwt(exp_in_sec: int) -> str:
    """构造一个能被 jwt_decode_exp 读到的 JWT（不验签）。"""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    payload_obj = {"sub": "42", "exp": int(time.time()) + exp_in_sec}
    payload = base64.urlsafe_b64encode(json.dumps(payload_obj).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.signature"


@pytest.fixture
def env_file(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text("")
    return p


@respx.mock
async def test_no_refresh_when_token_fresh(env_file: Path):
    fresh = _mk_jwt(3600)
    async with httpx.AsyncClient(base_url="http://x") as client:
        mgr = TokenManager(
            base_url="http://x",
            access_token=fresh,
            refresh_token=_mk_jwt(86400 * 6),
            env_path=env_file,
            raw_client=client,
        )
        token = await mgr.get_valid_access()
    assert token == fresh


@respx.mock
async def test_refresh_when_expiring(env_file: Path):
    expiring = _mk_jwt(60)  # < 5 min
    new_token = _mk_jwt(3600)
    route = respx.post("http://x/api/v1/auth/refresh").mock(
        return_value=httpx.Response(200, json={"access_token": new_token, "token_type": "bearer"})
    )
    async with httpx.AsyncClient(base_url="http://x") as client:
        mgr = TokenManager(
            base_url="http://x",
            access_token=expiring,
            refresh_token=_mk_jwt(86400 * 6),
            env_path=env_file,
            raw_client=client,
        )
        token = await mgr.get_valid_access()
    assert token == new_token
    assert route.called
    assert "THCCB_ACCESS_TOKEN" in env_file.read_text()


@respx.mock
async def test_concurrent_refresh_only_once(env_file: Path):
    expiring = _mk_jwt(60)
    new_token = _mk_jwt(3600)
    route = respx.post("http://x/api/v1/auth/refresh").mock(
        return_value=httpx.Response(200, json={"access_token": new_token, "token_type": "bearer"})
    )
    async with httpx.AsyncClient(base_url="http://x") as client:
        mgr = TokenManager(
            base_url="http://x",
            access_token=expiring,
            refresh_token=_mk_jwt(86400 * 6),
            env_path=env_file,
            raw_client=client,
        )
        results = await asyncio.gather(*[mgr.get_valid_access() for _ in range(10)])
    assert all(t == new_token for t in results)
    assert route.call_count == 1


@respx.mock
async def test_refresh_401_raises_fatal(env_file: Path):
    expiring = _mk_jwt(60)
    respx.post("http://x/api/v1/auth/refresh").mock(
        return_value=httpx.Response(401, json={"detail": "expired"})
    )
    async with httpx.AsyncClient(base_url="http://x") as client:
        mgr = TokenManager(
            base_url="http://x",
            access_token=expiring,
            refresh_token=_mk_jwt(86400 * 6),
            env_path=env_file,
            raw_client=client,
        )
        with pytest.raises(FatalAuthError):
            await mgr.get_valid_access()
