import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import pytest, pytest_asyncio, uuid
from decimal import Decimal
from datetime import datetime, timezone
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
            s.add(SiteConfig(key="loan_daily_rate", value="0.01", value_type="decimal"))
            s.add(SiteConfig(key="loan_leverage_k", value="1.0", value_type="decimal"))
            s.add(SiteConfig(key="loan_sweep_interval_sec", value="60", value_type="int"))


@pytest_asyncio.fixture
async def client():
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost:8004") as ac:
            yield ac


async def _make_user(cash=Decimal("0"), debt=Decimal("0"), superuser=False):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=f"u_{suffix}", email=f"{suffix}@t.com",
                    casdoor_id=f"cd_{suffix}", cash=cash, debt=debt, is_superuser=superuser)
            s.add(u)
            await s.flush()
            uid = u.id
    token = create_access_token(uid)
    return uid, {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_force_loan_requires_superuser(client):
    target_uid, _ = await _make_user()
    _, h = await _make_user(superuser=False)
    r = await client.post(f"/api/v1/user/{target_uid}/force-loan",
                          json={"amount": "100", "reason": "test"}, headers=h)
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_force_loan_grants_cash_and_debt(client):
    target_uid, _ = await _make_user(cash=Decimal("50"))
    _, h = await _make_user(superuser=True)
    r = await client.post(f"/api/v1/user/{target_uid}/force-loan",
                          json={"amount": "500", "reason": "活动奖励"}, headers=h)
    assert r.status_code == 200, r.text
    async with async_session_maker() as s:
        u = await s.get(User, target_uid)
    assert u.cash == Decimal("550.000000")
    assert u.debt == Decimal("500.000000")


@pytest.mark.asyncio
async def test_force_loan_blocked_when_disabled(client):
    async with async_session_maker() as s:
        async with s.begin():
            result = await s.execute(select(SiteConfig).where(SiteConfig.key == "loan_enabled"))
            row = result.scalar_one()
            row.value = "false"
            s.add(row)
    target_uid, _ = await _make_user()
    _, h = await _make_user(superuser=True)
    r = await client.post(f"/api/v1/user/{target_uid}/force-loan",
                          json={"amount": "100", "reason": "x"}, headers=h)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_forgive_debt_reduces_debt_no_cash_change(client):
    target_uid, _ = await _make_user(cash=Decimal("50"), debt=Decimal("200"))
    async with async_session_maker() as s:
        async with s.begin():
            u = await s.get(User, target_uid)
            u.debt_last_accrued_at = datetime.now(timezone.utc)
            s.add(u)
    _, h = await _make_user(superuser=True)
    r = await client.post(f"/api/v1/user/{target_uid}/forgive-debt",
                          json={"amount": "80", "reason": "compensation"}, headers=h)
    assert r.status_code == 200
    async with async_session_maker() as s:
        u = await s.get(User, target_uid)
    assert u.cash == Decimal("50.000000")
    # 允许微秒级 accrue
    assert abs(u.debt - Decimal("120")) < Decimal("0.01")


@pytest.mark.asyncio
async def test_forgive_overpay_clears_debt(client):
    target_uid, _ = await _make_user(cash=Decimal("50"), debt=Decimal("30"))
    async with async_session_maker() as s:
        async with s.begin():
            u = await s.get(User, target_uid)
            u.debt_last_accrued_at = datetime.now(timezone.utc)
            s.add(u)
    _, h = await _make_user(superuser=True)
    r = await client.post(f"/api/v1/user/{target_uid}/forgive-debt",
                          json={"amount": "9999", "reason": "full wipe"}, headers=h)
    assert r.status_code == 200
    async with async_session_maker() as s:
        u = await s.get(User, target_uid)
    assert u.debt == Decimal("0") or u.debt == Decimal("0.000000")
    assert u.debt_last_accrued_at is None
