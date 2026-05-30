import pytest
import pytest_asyncio
from decimal import Decimal
from datetime import datetime, timezone


@pytest_asyncio.fixture
async def session(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from sqlmodel import SQLModel
    from app.models import base, redemption, title, ledger  # noqa: F401 注册所有表

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_ledger_entry_roundtrip(session):
    from app.models.ledger import LedgerEntry, LEDGER_ENTRY_TYPES
    from sqlalchemy import select

    assert "borrow" in LEDGER_ENTRY_TYPES
    assert "admin_adjust_cash" in LEDGER_ENTRY_TYPES

    e = LedgerEntry(
        user_id=1,
        entry_type="borrow",
        cash_delta=Decimal("100.000000"),
        debt_delta=Decimal("100.000000"),
        cash_after=Decimal("600.000000"),
        debt_after=Decimal("100.000000"),
        debt_last_accrued_at_after=datetime(2026, 5, 30, tzinfo=timezone.utc),
        daily_rate_at_event=Decimal("0.01000000"),
        operator_user_id=None,
        reason=None,
    )
    session.add(e)
    await session.commit()

    row = (await session.execute(select(LedgerEntry))).scalars().first()
    assert row.entry_type == "borrow"
    assert row.cash_delta == Decimal("100.000000")
    assert row.debt_after == Decimal("100.000000")
    assert row.daily_rate_at_event == Decimal("0.01000000")
    assert row.id is not None
    assert row.created_at is not None
