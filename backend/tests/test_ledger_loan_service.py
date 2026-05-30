import pytest
import pytest_asyncio
from decimal import Decimal
from datetime import datetime, timezone


@pytest_asyncio.fixture
async def session_and_user(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from sqlmodel import SQLModel
    from app.models import base, redemption, title, ledger  # noqa: F401
    from app.models.base import User

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        u = User(username="u1", cash=Decimal("1000"), debt=Decimal("0"))
        s.add(u)
        await s.commit()
        await s.refresh(u)
        uid = u.id
    yield maker, uid
    await engine.dispose()


@pytest.mark.asyncio
async def test_increase_debt_writes_borrow_ledger(session_and_user):
    from app.services import loan_service
    from app.models.ledger import LedgerEntry
    from sqlalchemy import select

    maker, uid = session_and_user
    async with maker() as s:
        await loan_service.increase_debt(
            s, uid, Decimal("200"), grant_cash=True, daily_rate=Decimal("0.01"),
            source="borrow", operator_user_id=None,
        )
        await s.commit()
    async with maker() as s:
        row = (await s.execute(select(LedgerEntry))).scalars().first()
        assert row.entry_type == "borrow"
        assert row.debt_delta == Decimal("200.000000")
        assert row.cash_delta == Decimal("200.000000")
        assert row.debt_after == Decimal("200.000000")
        assert row.cash_after == Decimal("1200.000000")
        assert row.daily_rate_at_event == Decimal("0.01000000")


@pytest.mark.asyncio
async def test_decrease_debt_writes_repay_ledger(session_and_user):
    from app.services import loan_service
    from app.models.ledger import LedgerEntry
    from sqlalchemy import select

    maker, uid = session_and_user
    async with maker() as s:
        await loan_service.increase_debt(
            s, uid, Decimal("200"), grant_cash=True, daily_rate=Decimal("0"),
            source="borrow", operator_user_id=None,
        )
        await s.commit()
    async with maker() as s:
        await loan_service.decrease_debt(
            s, uid, Decimal("50"), consume_cash=True, daily_rate=Decimal("0"),
            source="repay", operator_user_id=None,
        )
        await s.commit()
    async with maker() as s:
        rows = (await s.execute(
            select(LedgerEntry).order_by(LedgerEntry.id)
        )).scalars().all()
        assert len(rows) == 2
        repay = rows[1]
        assert repay.entry_type == "repay"
        assert repay.debt_delta == Decimal("-50.000000")
        assert repay.cash_delta == Decimal("-50.000000")
        assert repay.debt_after == Decimal("150.000000")


@pytest.mark.asyncio
async def test_decrease_debt_locked_does_NOT_write_ledger(session_and_user):
    """liquidation 直接调 decrease_debt_locked，不应产生 ledger 行（它有 LiquidationEvent）。"""
    from app.services import loan_service
    from app.models.ledger import LedgerEntry
    from app.models.base import User
    from sqlalchemy import select

    maker, uid = session_and_user
    async with maker() as s:
        await loan_service.increase_debt(
            s, uid, Decimal("200"), grant_cash=True, daily_rate=Decimal("0"),
            source="borrow", operator_user_id=None,
        )
        await s.commit()
    async with maker() as s:
        u = (await s.execute(select(User).where(User.id == uid))).scalars().first()
        await loan_service.decrease_debt_locked(
            s, u, Decimal("30"), consume_cash=True, daily_rate=Decimal("0"),
        )
        await s.commit()
    async with maker() as s:
        rows = (await s.execute(select(LedgerEntry))).scalars().all()
        assert len(rows) == 1
        assert rows[0].entry_type == "borrow"
