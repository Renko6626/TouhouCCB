import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest, pytest_asyncio, uuid
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from sqlmodel import SQLModel
from app.core.database import engine, async_session_maker
from app.models.base import User
from app.services.loan_service import accrue_interest


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
        await conn.run_sync(SQLModel.metadata.create_all)


def _new_user_sync(cash=Decimal("100"), debt=Decimal("0"), last_accrued=None):
    return User(
        username=f"u_{uuid.uuid4().hex[:6]}",
        email=f"{uuid.uuid4().hex[:6]}@t.com",
        casdoor_id=uuid.uuid4().hex,
        cash=cash,
        debt=debt,
        debt_last_accrued_at=last_accrued,
    )


def test_accrue_noop_when_debt_zero():
    u = _new_user_sync(debt=Decimal("0"), last_accrued=datetime.now(timezone.utc) - timedelta(hours=1))
    before = u.debt
    accrue_interest(u, Decimal("0.01"), datetime.now(timezone.utc))
    assert u.debt == before


def test_accrue_noop_when_last_accrued_none():
    u = _new_user_sync(debt=Decimal("100"), last_accrued=None)
    before = u.debt
    accrue_interest(u, Decimal("0.01"), datetime.now(timezone.utc))
    assert u.debt == before


def test_accrue_one_full_day_at_1pct():
    now = datetime.now(timezone.utc)
    u = _new_user_sync(debt=Decimal("1000"), last_accrued=now - timedelta(days=1))
    accrue_interest(u, Decimal("0.01"), now)
    # 线性近似一天：1000 * (1 + 0.01) = 1010
    assert abs(u.debt - Decimal("1010")) < Decimal("0.01")
    assert u.debt_last_accrued_at == now


def test_accrue_compounds_across_sweeps():
    """10 次连续 sweep，每次 60s，每次作用在当前 debt 上（复利）。"""
    now = datetime.now(timezone.utc)
    u = _new_user_sync(debt=Decimal("1000"), last_accrued=now)
    rate = Decimal("0.01")
    for i in range(10):
        now = now + timedelta(seconds=60)
        accrue_interest(u, rate, now)
    expected = Decimal("1000") * (Decimal(1) + rate * Decimal(60) / Decimal(86400)) ** 10
    assert abs(u.debt - expected) < Decimal("0.0001")


def test_accrue_negative_elapsed_noop():
    """时钟倒退不应倒扣利息（安全兜底）。"""
    now = datetime.now(timezone.utc)
    u = _new_user_sync(debt=Decimal("1000"), last_accrued=now + timedelta(hours=1))
    before = u.debt
    accrue_interest(u, Decimal("0.01"), now)
    assert u.debt == before
