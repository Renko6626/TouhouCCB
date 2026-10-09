import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import pytest, pytest_asyncio, uuid
from sqlalchemy import select
from decimal import Decimal
from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import User, SiteConfig


@pytest_asyncio.fixture(autouse=True)
async def _seed_loan_config(setup_db):
    """conftest 的 setup_db 负责清库；此 fixture 仅追加 SiteConfig 种子。"""
    async with async_session_maker() as s:
        async with s.begin():
            s.add(SiteConfig(key="loan_daily_rate", value="0.01", value_type="decimal"))
            s.add(SiteConfig(key="loan_sweep_interval_sec", value="60", value_type="int"))
            s.add(SiteConfig(key="loan_enabled", value="true", value_type="bool"))
            for key, value in (("credit_leverage", "2"), ("credit_maintenance_ratio", "0.2")):
                existing = (await s.execute(select(SiteConfig).where(SiteConfig.key == key))).scalar_one_or_none()
                if existing is None:
                    s.add(SiteConfig(key=key, value=value, value_type="decimal"))


async def _make_user(superuser=False):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=f"u_{suffix}", email=f"{suffix}@t.com",
                    casdoor_id=f"cd_{suffix}", cash=Decimal("0"), is_superuser=superuser)
            s.add(u)
            await s.flush()
            uid = u.id
    token = create_access_token(uid)
    return uid, {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_list_requires_superuser(client):
    _, h = await _make_user(superuser=False)
    r = await client.get("/api/v1/admin/site-config", headers=h)
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_list_returns_editable_keys(client):
    async with async_session_maker() as s:
        async with s.begin():
            s.add_all([
                SiteConfig(key="fx_enabled", value="false", value_type="bool"),
                SiteConfig(key="pve_enabled", value="false", value_type="bool"),
                SiteConfig(key="single_writer_enabled", value="true", value_type="bool"),
            ])
    _, h = await _make_user(superuser=True)
    r = await client.get("/api/v1/admin/site-config", headers=h)
    assert r.status_code == 200
    keys = {item["key"] for item in r.json()}
    assert {"loan_enabled", "credit_leverage", "loan_daily_rate", "loan_sweep_interval_sec"} <= keys
    assert not keys.intersection({"fx_enabled", "pve_enabled", "single_writer_enabled"})


@pytest.mark.asyncio
async def test_update_operator_gate_value(client):
    _, h = await _make_user(superuser=True)
    r = await client.put("/api/v1/admin/site-config/loan_enabled", json={"value": "false"}, headers=h)
    assert r.status_code == 200
    r2 = await client.get("/api/v1/admin/site-config", headers=h)
    rates = {i["key"]: i["value"] for i in r2.json()}
    assert rates["loan_enabled"] == "false"


@pytest.mark.asyncio
async def test_update_rejects_unknown_key(client):
    _, h = await _make_user(superuser=True)
    r = await client.put("/api/v1/admin/site-config/not_whitelisted", json={"value": "x"}, headers=h)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_update_rejects_invalid_decimal(client):
    _, h = await _make_user(superuser=True)
    r = await client.put("/api/v1/admin/site-config/loan_daily_rate", json={"value": "abc"}, headers=h)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_update_rejects_rate_out_of_range(client):
    _, h = await _make_user(superuser=True)
    r = await client.put("/api/v1/admin/site-config/loan_daily_rate", json={"value": "2.0"}, headers=h)
    assert r.status_code == 400  # 要求 rate ∈ (0, 1)
