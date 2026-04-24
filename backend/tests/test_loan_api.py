import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest, pytest_asyncio, uuid
from decimal import Decimal
from httpx import AsyncClient, ASGITransport
from asgi_lifespan import LifespanManager
from sqlmodel import SQLModel, select
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
            s.add(SiteConfig(key="loan_enabled", value="true", value_type="bool"))
            s.add(SiteConfig(key="loan_leverage_k", value="1.0", value_type="decimal"))
            s.add(SiteConfig(key="loan_daily_rate", value="0.01", value_type="decimal"))
            s.add(SiteConfig(key="loan_sweep_interval_sec", value="60", value_type="int"))


@pytest_asyncio.fixture
async def client():
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost:8004") as ac:
            yield ac


async def _make_user(cash=Decimal("1000"), debt=Decimal("0"), superuser=False):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(
                username=f"u_{suffix}",
                email=f"{suffix}@t.com",
                casdoor_id=f"cd_{suffix}",
                cash=cash,
                debt=debt,
                is_superuser=superuser,
            )
            s.add(u)
            await s.flush()
            uid = u.id
    token = create_access_token(uid)
    return uid, {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_quota_returns_fields(client):
    _, h = await _make_user(cash=Decimal("500"))
    r = await client.get("/api/v1/loan/quota", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is True
    assert Decimal(body["cash"]) == Decimal("500")
    assert Decimal(body["debt"]) == Decimal("0")
    assert Decimal(body["max_borrow"]) >= Decimal("500")
    assert Decimal(body["daily_rate"]) == Decimal("0.01")


@pytest.mark.asyncio
async def test_borrow_success_updates_cash_and_debt(client):
    uid, h = await _make_user(cash=Decimal("1000"))
    r = await client.post("/api/v1/loan/borrow", json={"amount": "500"}, headers=h)
    assert r.status_code == 200, r.text
    async with async_session_maker() as s:
        u = await s.get(User, uid)
    assert u.cash == Decimal("1500.000000")
    assert u.debt == Decimal("500.000000")
    assert u.debt_last_accrued_at is not None


@pytest.mark.asyncio
async def test_borrow_exceeds_quota_400(client):
    _, h = await _make_user(cash=Decimal("100"))
    # k=1, net_worth=100, max_borrow=100；借 200 应拒
    r = await client.post("/api/v1/loan/borrow", json={"amount": "200"}, headers=h)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_borrow_when_disabled_403(client):
    async with async_session_maker() as s:
        async with s.begin():
            result = await s.execute(select(SiteConfig).where(SiteConfig.key == "loan_enabled"))
            row = result.scalar_one()
            row.value = "false"
            s.add(row)
    _, h = await _make_user(cash=Decimal("1000"))
    r = await client.post("/api/v1/loan/borrow", json={"amount": "100"}, headers=h)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_borrow_nonpositive_422(client):
    _, h = await _make_user(cash=Decimal("1000"))
    r = await client.post("/api/v1/loan/borrow", json={"amount": "0"}, headers=h)
    assert r.status_code == 422  # pydantic gt=0


from datetime import datetime, timezone


@pytest.mark.asyncio
async def test_repay_partial(client):
    uid, h = await _make_user(cash=Decimal("1000"), debt=Decimal("200"))
    async with async_session_maker() as s:
        async with s.begin():
            u = await s.get(User, uid)
            u.debt_last_accrued_at = datetime.now(timezone.utc)
            s.add(u)
    r = await client.post("/api/v1/loan/repay", json={"amount": "50"}, headers=h)
    assert r.status_code == 200, r.text
    async with async_session_maker() as s:
        u2 = await s.get(User, uid)
    assert u2.cash == Decimal("950.000000")
    # 允许微秒级 accrue 微增
    assert abs(u2.debt - Decimal("150")) < Decimal("0.01")


@pytest.mark.asyncio
async def test_repay_overpay_clamps_to_debt(client):
    uid, h = await _make_user(cash=Decimal("1000"), debt=Decimal("50"))
    async with async_session_maker() as s:
        async with s.begin():
            u = await s.get(User, uid)
            u.debt_last_accrued_at = datetime.now(timezone.utc)
            s.add(u)
    r = await client.post("/api/v1/loan/repay", json={"amount": "9999"}, headers=h)
    assert r.status_code == 200
    async with async_session_maker() as s:
        u2 = await s.get(User, uid)
    assert u2.debt == Decimal("0") or u2.debt == Decimal("0.000000")
    # cash = 1000 - effective（effective 约等于 50，允许微增）
    assert abs(u2.cash - Decimal("950")) < Decimal("0.01")
    assert u2.debt_last_accrued_at is None


@pytest.mark.asyncio
async def test_repay_exceeds_cash_400(client):
    _, h = await _make_user(cash=Decimal("30"), debt=Decimal("200"))
    r = await client.post("/api/v1/loan/repay", json={"amount": "100"}, headers=h)
    assert r.status_code == 400
