import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest, pytest_asyncio
from decimal import Decimal
from app.core.database import async_session_maker
from app.models.base import SiteConfig
from app.services.site_config import (
    get_decimal, get_int, get_bool, set_value, SiteConfigError,
)


@pytest_asyncio.fixture(autouse=True)
async def _seed_x_config(setup_db):
    """conftest 的 setup_db 负责清库；此 fixture 仅追加 x_* 种子。"""
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


@pytest.mark.asyncio
async def test_liquidation_site_config_defaults_loaded(client):
    """liquidation_* 4 个 key 应在初始化后默认存在。"""
    from app.services.loan_migrate import auto_migrate
    # setup_db 已 drop_all + create_all，需重新 auto_migrate 才有默认值
    await auto_migrate()

    async with async_session_maker() as db:
        enabled = await get_bool(db, "liquidation_enabled")
        interval = await get_int(db, "liquidation_sweep_interval_sec")
        hard = await get_decimal(db, "liquidation_hard_threshold")
        soft = await get_decimal(db, "liquidation_soft_threshold")

    assert enabled is False, "默认应关，灰度开启"
    assert interval == 600
    assert hard == Decimal("0.2")
    assert soft == Decimal("0.5")


@pytest.mark.asyncio
async def test_liquidation_site_config_keys_in_allowed():
    """site_config.ALLOWED_KEYS 必须含这 4 个 key 才能通过 admin API 改。"""
    from app.api.v1.site_config import _WHITELIST
    assert "liquidation_enabled" in _WHITELIST
    assert "liquidation_sweep_interval_sec" in _WHITELIST
    assert "liquidation_hard_threshold" in _WHITELIST
    assert "liquidation_soft_threshold" in _WHITELIST
