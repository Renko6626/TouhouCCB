import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import pytest, pytest_asyncio, uuid
from decimal import Decimal
from httpx import AsyncClient, ASGITransport
from asgi_lifespan import LifespanManager
from sqlmodel import SQLModel
from app.main import app
from app.core.database import engine, async_session_maker
from app.core.users import create_access_token
from app.models.base import User, SiteConfig


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
        await conn.run_sync(SQLModel.metadata.create_all)
    async with async_session_maker() as s:
        async with s.begin():
            s.add(SiteConfig(key="loan_daily_rate", value="0.01", value_type="decimal"))
            s.add(SiteConfig(key="loan_sweep_interval_sec", value="60", value_type="int"))
            s.add(SiteConfig(key="loan_enabled", value="true", value_type="bool"))
            s.add(SiteConfig(key="loan_leverage_k", value="1.0", value_type="decimal"))


@pytest_asyncio.fixture
async def client():
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost:8004") as ac:
            yield ac


async def _make_user(superuser=False):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=f"u_{suffix}", email=f"{suffix}@t.com",
                    casdoor_id=f"cd_{suffix}", cash=Decimal("0"), is_superuser=superuser)
            s.add(u)
            await s.flush()
            uid = u.id
    token = create_access_token(uid)
    return uid, {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_list_requires_superuser(client):
    _, h = await _make_user(superuser=False)
    r = await client.get("/api/v1/admin/site-config", headers=h)
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_list_returns_all_keys(client):
    _, h = await _make_user(superuser=True)
    r = await client.get("/api/v1/admin/site-config", headers=h)
    assert r.status_code == 200
    keys = {item["key"] for item in r.json()}
    assert {"loan_enabled", "loan_leverage_k", "loan_daily_rate", "loan_sweep_interval_sec"} <= keys


@pytest.mark.asyncio
async def test_update_decimal_value(client):
    _, h = await _make_user(superuser=True)
    r = await client.put("/api/v1/admin/site-config/loan_daily_rate", json={"value": "0.05"}, headers=h)
    assert r.status_code == 200
    r2 = await client.get("/api/v1/admin/site-config", headers=h)
    rates = {i["key"]: i["value"] for i in r2.json()}
    assert rates["loan_daily_rate"] == "0.05"


@pytest.mark.asyncio
async def test_update_rejects_unknown_key(client):
    _, h = await _make_user(superuser=True)
    r = await client.put("/api/v1/admin/site-config/not_whitelisted", json={"value": "x"}, headers=h)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_update_rejects_invalid_decimal(client):
    _, h = await _make_user(superuser=True)
    r = await client.put("/api/v1/admin/site-config/loan_daily_rate", json={"value": "abc"}, headers=h)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_update_rejects_rate_out_of_range(client):
    _, h = await _make_user(superuser=True)
    r = await client.put("/api/v1/admin/site-config/loan_daily_rate", json={"value": "2.0"}, headers=h)
    assert r.status_code == 400  # 要求 rate ∈ (0, 1)
