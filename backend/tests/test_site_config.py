import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest, pytest_asyncio
from decimal import Decimal
from sqlmodel import SQLModel
from app.core.database import engine, async_session_maker
from app.models.base import SiteConfig
from app.services.site_config import (
    get_decimal, get_int, get_bool, set_value, SiteConfigError,
)


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    # 每次重建表保证干净（仅 sqlite 测试库）
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
        await conn.run_sync(SQLModel.metadata.create_all)
    async with async_session_maker() as s:
        async with s.begin():
            s.add(SiteConfig(key="x_rate", value="0.05", value_type="decimal"))
            s.add(SiteConfig(key="x_interval", value="30", value_type="int"))
            s.add(SiteConfig(key="x_flag", value="true", value_type="bool"))


@pytest.mark.asyncio
async def test_get_decimal_returns_decimal():
    async with async_session_maker() as s:
        v = await get_decimal(s, "x_rate")
    assert v == Decimal("0.05")


@pytest.mark.asyncio
async def test_get_int_returns_int():
    async with async_session_maker() as s:
        v = await get_int(s, "x_interval")
    assert v == 30


@pytest.mark.asyncio
async def test_get_bool_true_false():
    async with async_session_maker() as s:
        v = await get_bool(s, "x_flag")
    assert v is True


@pytest.mark.asyncio
async def test_missing_key_raises():
    async with async_session_maker() as s:
        with pytest.raises(SiteConfigError):
            await get_decimal(s, "nope")


@pytest.mark.asyncio
async def test_set_value_updates_row():
    async with async_session_maker() as s:
        await set_value(s, "x_rate", "0.1", admin_user_id=None)
    async with async_session_maker() as s:
        v = await get_decimal(s, "x_rate")
    assert v == Decimal("0.1")
