import pytest
import pytest_asyncio
from sqlmodel import select
from sqlalchemy.exc import IntegrityError

from app.core.database import async_session_maker
from app.models.base import User
from app.models.title import (
    Title, UserTitle, TitleCodeBatch, TitleCode, MarketRequiredTitle,
)


@pytest.mark.asyncio
async def test_create_title_and_query(setup_db):
    async with async_session_maker() as s:
        async with s.begin():
            t = Title(name="VIP", description="尊贵会员",
                      color="#FFD700", icon="★", sort_order=10)
            s.add(t)
            await s.flush()
            tid = t.id

    async with async_session_maker() as s:
        got = (await s.execute(select(Title).where(Title.id == tid))).scalar_one()
        assert got.name == "VIP"
        assert got.is_active is True


@pytest.mark.asyncio
async def test_title_name_unique(setup_db):
    async with async_session_maker() as s:
        async with s.begin():
            s.add(Title(name="VIP"))
    with pytest.raises(IntegrityError):
        async with async_session_maker() as s:
            async with s.begin():
                s.add(Title(name="VIP"))


@pytest.mark.asyncio
async def test_user_title_unique_per_pair(setup_db):
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username="alice", casdoor_id="cd_a")
            t = Title(name="VIP")
            s.add(u); s.add(t)
            await s.flush()
            s.add(UserTitle(user_id=u.id, title_id=t.id, source="admin"))
    with pytest.raises(IntegrityError):
        async with async_session_maker() as s:
            async with s.begin():
                u = (await s.execute(select(User).where(User.username == "alice"))).scalar_one()
                t = (await s.execute(select(Title).where(Title.name == "VIP"))).scalar_one()
                s.add(UserTitle(user_id=u.id, title_id=t.id, source="code"))


@pytest.mark.asyncio
async def test_user_equipped_title_id_nullable(setup_db):
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username="bob", casdoor_id="cd_b")
            s.add(u)
            await s.flush()
            assert u.equipped_title_id is None
