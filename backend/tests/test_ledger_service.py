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
        u = User(username="u1", cash=Decimal("600"), debt=Decimal("100"),
                 debt_last_accrued_at=datetime(2026, 5, 30, tzinfo=timezone.utc))
        s.add(u)
        await s.commit()
        await s.refresh(u)
        yield s, u
    await engine.dispose()


@pytest.mark.asyncio
async def test_record_entry_reads_snapshot_from_user(session_and_user):
    from app.services.ledger_service import record_entry
    from app.models.ledger import LedgerEntry
    from sqlalchemy import select

    s, u = session_and_user
    await record_entry(
        s, user=u, entry_type="borrow",
        cash_delta=Decimal("100"), debt_delta=Decimal("100"),
        daily_rate=Decimal("0.01"),
    )
    await s.commit()

    row = (await s.execute(select(LedgerEntry))).scalars().first()
    assert row.entry_type == "borrow"
    assert row.cash_after == Decimal("600.000000")    # 从 user 快照读
    assert row.debt_after == Decimal("100.000000")
    assert row.daily_rate_at_event == Decimal("0.01000000")
    assert row.operator_user_id is None
    from app.models.audit import AuditEvent
    ev = (await s.execute(select(AuditEvent))).scalars().one()
    assert ev.event_type == "loan_borrow" and ev.ref_table == "ledger_entry" and ev.ref_id == row.id


@pytest.mark.asyncio
async def test_record_entry_rejects_bad_type(session_and_user):
    from app.services.ledger_service import record_entry
    s, u = session_and_user
    with pytest.raises(ValueError):
        await record_entry(s, user=u, entry_type="nonsense",
                     cash_delta=Decimal("0"), debt_delta=Decimal("0"),
                     daily_rate=None)
