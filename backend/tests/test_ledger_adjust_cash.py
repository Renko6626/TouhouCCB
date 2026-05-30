import pytest
import pytest_asyncio
from decimal import Decimal
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.core.database import get_async_session
from app.core.users import current_superuser


@pytest_asyncio.fixture
async def client_ctx(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from sqlmodel import SQLModel
    from app.models import base, redemption, title, ledger  # noqa: F401
    from app.models.base import User

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        u1 = User(username="u1", cash=Decimal("500"), debt=Decimal("0"))
        admin = User(username="admin", cash=Decimal("0"), debt=Decimal("0"), is_superuser=True)
        s.add(u1); s.add(admin)
        await s.commit(); await s.refresh(u1); await s.refresh(admin)
        u1_id, admin_id, admin_obj = u1.id, admin.id, admin

    async def _sess():
        async with maker() as s:
            yield s
    app.dependency_overrides[get_async_session] = _sess
    app.dependency_overrides[current_superuser] = lambda: admin_obj

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, maker, u1_id, admin_id
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_adjust_cash_writes_ledger(client_ctx):
    from app.models.ledger import LedgerEntry
    from sqlalchemy import select

    ac, maker, u1_id, admin_id = client_ctx
    r = await ac.post(f"/api/v1/user/{u1_id}/adjust-cash",
                      json={"amount": "100", "reason": "活动奖励"})
    assert r.status_code == 200

    async with maker() as s:
        row = (await s.execute(select(LedgerEntry))).scalars().first()
        assert row is not None
        assert row.entry_type == "admin_adjust_cash"
        assert row.cash_delta == Decimal("100.000000")
        assert row.debt_delta == Decimal("0.000000")
        assert row.cash_after == Decimal("600.000000")
        assert row.operator_user_id == admin_id
        assert row.reason == "活动奖励"
