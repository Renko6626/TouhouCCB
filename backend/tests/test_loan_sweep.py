import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest, pytest_asyncio, uuid
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from sqlmodel import SQLModel
from app.core.database import engine, async_session_maker
from app.models.base import User, SiteConfig
from app.services.loan_sweep import run_sweep_once


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
        await conn.run_sync(SQLModel.metadata.create_all)
    async with async_session_maker() as s:
        async with s.begin():
            s.add(SiteConfig(key="loan_daily_rate", value="0.01", value_type="decimal"))


async def _seed_user(debt, last_accrued):
    async with async_session_maker() as s:
        async with s.begin():
            u = User(
                username=f"u_{uuid.uuid4().hex[:6]}",
                email=f"{uuid.uuid4().hex[:6]}@t.com",
                casdoor_id=uuid.uuid4().hex,
                cash=Decimal("0"),
                debt=Decimal(debt),
                debt_last_accrued_at=last_accrued,
            )
            s.add(u)
            await s.flush()
            return u.id


@pytest.mark.asyncio
async def test_sweep_skips_users_with_zero_debt():
    uid = await _seed_user(debt="0", last_accrued=None)
    await run_sweep_once()
    async with async_session_maker() as s:
        u = await s.get(User, uid)
    assert u.debt == Decimal("0")


@pytest.mark.asyncio
async def test_sweep_accrues_interest():
    now = datetime.now(timezone.utc)
    uid = await _seed_user(debt="1000", last_accrued=now - timedelta(hours=1))
    await run_sweep_once()
    async with async_session_maker() as s:
        u = await s.get(User, uid)
    # 1h @ 1%/day ≈ 1000 * (1 + 0.01/24) ≈ 1000.4167
    assert u.debt > Decimal("1000.3")
    assert u.debt < Decimal("1000.5")
    assert u.debt_last_accrued_at is not None


@pytest.mark.asyncio
async def test_sweep_multiple_users_independent():
    now = datetime.now(timezone.utc)
    uid1 = await _seed_user(debt="100", last_accrued=now - timedelta(days=1))
    uid2 = await _seed_user(debt="0", last_accrued=None)
    await run_sweep_once()
    async with async_session_maker() as s:
        u1 = await s.get(User, uid1)
        u2 = await s.get(User, uid2)
    assert u1.debt > Decimal("100.5") and u1.debt < Decimal("101.5")
    assert u2.debt == Decimal("0")
