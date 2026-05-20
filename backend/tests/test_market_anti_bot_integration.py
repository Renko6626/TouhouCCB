import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import time
import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlmodel import select

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import Market, MarketStatus, Outcome, SiteConfig, User
from app.services.anti_bot import generate_client_token_for_test
from app.services.loan_migrate import auto_migrate


async def _set_site_config(key: str, value: str) -> None:
    async with async_session_maker() as s:
        async with s.begin():
            result = await s.execute(select(SiteConfig).where(SiteConfig.key == key))
            row = result.scalars().first()
            if row is not None:
                row.value = value


TEST_SECRET = "test_secret_32_bytes_hex_abcdef"


async def _seed_user_and_market():
    # setup_db fixture drops+creates tables; re-run auto_migrate to seed SiteConfig defaults
    await auto_migrate()
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(
                username=f"ab_{suffix}", email=f"{suffix}@t.com",
                casdoor_id=f"ab_cas_{suffix}", cash=Decimal("1000"),
            )
            s.add(u)
            await s.flush()
            m = Market(title=f"ab_m_{suffix}", description="",
                       liquidity_b=100.0, status=MarketStatus.TRADING, tags="")
            s.add(m)
            await s.flush()
            o = Outcome(market_id=m.id, label="A", total_shares=Decimal("0"))
            s.add(o)
            await s.flush()
            return u.id, m.id, o.id


def _auth_header(uid: int) -> dict:
    return {"Authorization": f"Bearer {create_access_token(uid)}"}


def _client_token_headers(uid: int) -> dict:
    ts = int(time.time())
    token = generate_client_token_for_test(TEST_SECRET, uid, ts)
    return {"X-Client-Token": token, "X-Client-TS": str(ts)}


@pytest.mark.asyncio
async def test_buy_passes_when_activity_mode_off(client):
    """默认 activity_mode=false → 不带 X-Client-Token 也能调 /quote。"""
    uid, mid, oid = await _seed_user_and_market()
    r = await client.post(
        "/api/v1/market/quote",
        headers=_auth_header(uid),
        json={"outcome_id": oid, "shares": 1.0, "side": "buy"},
    )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_buy_403_when_activity_mode_on_no_header(client):
    uid, mid, oid = await _seed_user_and_market()
    await _set_site_config("activity_mode_enabled", "true")
    from app.services.site_config import clear_cache
    clear_cache()

    with patch("app.services.anti_bot.settings") as m:
        m.CLIENT_TOKEN_SECRET = TEST_SECRET
        r = await client.post(
            "/api/v1/market/quote",
            headers=_auth_header(uid),
            json={"outcome_id": oid, "shares": 1.0, "side": "buy"},
        )
    assert r.status_code == 403, r.text
    assert "anti-bot" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_buy_passes_when_activity_mode_on_with_valid_token(client):
    uid, mid, oid = await _seed_user_and_market()
    await _set_site_config("activity_mode_enabled", "true")
    from app.services.site_config import clear_cache
    clear_cache()

    with patch("app.services.anti_bot.settings") as m:
        m.CLIENT_TOKEN_SECRET = TEST_SECRET
        headers = _auth_header(uid)
        headers.update(_client_token_headers(uid))
        r = await client.post(
            "/api/v1/market/quote",
            headers=headers,
            json={"outcome_id": oid, "shares": 1.0, "side": "buy"},
        )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_buy_passes_when_user_in_whitelist(client):
    """白名单 user 不带 token 也能通过。"""
    uid, mid, oid = await _seed_user_and_market()
    await _set_site_config("activity_mode_enabled", "true")
    await _set_site_config("quant_whitelist_user_ids", str(uid))
    from app.services.site_config import clear_cache
    clear_cache()

    with patch("app.services.anti_bot.settings") as m:
        m.CLIENT_TOKEN_SECRET = TEST_SECRET
        r = await client.post(
            "/api/v1/market/quote",
            headers=_auth_header(uid),
            json={"outcome_id": oid, "shares": 1.0, "side": "buy"},
        )
    assert r.status_code == 200, r.text
