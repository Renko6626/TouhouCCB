import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest, pytest_asyncio, uuid
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from app.core.database import async_session_maker
from app.models.base import User, SiteConfig
from app.services.loan_sweep import run_sweep_once
from sqlalchemy import select


@pytest_asyncio.fixture(autouse=True)
async def _seed_loan_rate(setup_db):
    """conftest 的 setup_db 负责清库；此 fixture 仅追加 loan_daily_rate 种子。"""
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


@pytest.mark.asyncio
async def test_sweep_skips_recently_accrued_users():
    """审计 M1：距上次结息不足 loan_sweep_min_accrual_sec（默认 3600）的用户本 tick 跳过，
    不写 interest_accrual 事件；超过窗口的照常结息。"""
    from app.models.audit import AuditEvent
    now = datetime.now(timezone.utc)
    recent = await _seed_user(debt="1000", last_accrued=now - timedelta(minutes=5))
    stale = await _seed_user(debt="1000", last_accrued=now - timedelta(hours=2))
    touched = await run_sweep_once()
    assert touched == 1
    async with async_session_maker() as s:
        assert (await s.get(User, recent)).debt == Decimal("1000")
        assert (await s.get(User, stale)).debt > Decimal("1000")
        evs = (await s.execute(select(AuditEvent).where(AuditEvent.event_type == "interest_accrual"))).scalars().all()
        assert [e.user_id for e in evs] == [stale]
