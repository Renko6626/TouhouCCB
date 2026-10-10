"""WP1：统一信贷配置键、迁移种子、flags 缓存与 admin 交叉校验（计划 §3.4 / F6 / F7）。"""
import os
import sys
from decimal import Decimal
from uuid import uuid4

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
import pytest_asyncio
from sqlalchemy import select, delete

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.audit import AuditEvent
from app.models.base import SiteConfig, User
from app.services import site_config as site_config_service
from app.services.credit import flags as credit_flags
from app.services.loan_migrate import auto_migrate, seed_credit_risk_configs


@pytest_asyncio.fixture(autouse=True)
async def _reset_flags(setup_db):
    async with async_session_maker() as s:
        await s.execute(delete(SiteConfig))
        await s.commit()
    credit_flags.clear_flags()
    yield
    credit_flags.clear_flags()


@pytest.mark.parametrize("raw", [{}, {"credit_leverage":"abc", "credit_maintenance_ratio":".2"}, {"credit_leverage":"51", "credit_maintenance_ratio":".01"}, {"credit_leverage":"2", "credit_maintenance_ratio":"1"}])
def test_parse_flags_rejects_invalid_persisted_config(raw):
    with pytest.raises(credit_flags.CreditConfigError):
        credit_flags.parse_flags(raw)


def test_parse_flags_valid_thresholds():
    flags = credit_flags.parse_flags({"credit_leverage":"20", "credit_maintenance_ratio":".04", "credit_new_risk_frozen":"true"})
    assert flags.thresholds.r_initial == Decimal(1) / Decimal(19)
    assert flags.credit_new_risk_frozen is True


async def _seed_config(key, value, value_type="decimal"):
    async with async_session_maker() as s:
        async with s.begin():
            s.add(SiteConfig(key=key, value=value, value_type=value_type))


async def _config_value(key):
    async with async_session_maker() as s:
        return (await s.execute(select(SiteConfig.value).where(SiteConfig.key == key))).scalar_one_or_none()


@pytest.mark.asyncio
async def test_fresh_config_defaults_and_idempotence(setup_db):
    await auto_migrate()
    assert await _config_value("credit_leverage") == "10"
    assert await _config_value("credit_maintenance_ratio") == "0.04"
    assert await _config_value("loan_enabled") == "true"
    assert await seed_credit_risk_configs() == []
    async with async_session_maker() as s:
        flags = await credit_flags.load_flags(s)
    assert flags.thresholds.r_maintenance == Decimal(".04")
    # Fresh-install policy grants 500 equity up to 4,500 debt (10x assets),
    # while persisting the same settings across a second startup.
    assert flags.thresholds.max_new_gold_loan(
        equity=Decimal("500"), debt=Decimal("0"),
        positive_assets=Decimal("0"), short_cover=Decimal("0"),
    ) == Decimal("4500.000000")


@pytest.mark.asyncio
async def test_existing_params_and_operator_gates_preserved(setup_db):
    for key, value in {"credit_leverage":"3", "credit_maintenance_ratio":".2", "loan_leverage_k":"9", "liquidation_hard_threshold":".05", "unified_credit_enabled":"false", "loan_enabled":"false", "credit_new_risk_frozen":"true"}.items():
        await _seed_config(key, value)
    await auto_migrate()
    assert await seed_credit_risk_configs() == []
    assert await _config_value("credit_leverage") == "3"
    assert await _config_value("credit_maintenance_ratio") == ".2"
    assert await _config_value("loan_enabled") == "false"
    assert await _config_value("credit_new_risk_frozen") == "true"
    assert await _config_value("unified_credit_enabled") is None
    assert await _config_value("loan_leverage_k") is None


@pytest.mark.asyncio
async def test_missing_params_map_legacy_once(setup_db):
    await _seed_config("loan_leverage_k", "3")
    await _seed_config("liquidation_hard_threshold", ".15")
    assert set(await seed_credit_risk_configs()) == {"credit_leverage", "credit_maintenance_ratio"}
    assert await _config_value("credit_leverage") == "4"
    assert await _config_value("credit_maintenance_ratio") == "0.15"
    assert await seed_credit_risk_configs() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("values", [{"loan_leverage_k":"50", "liquidation_hard_threshold":".2"}, {"credit_leverage":"abc", "credit_maintenance_ratio":".2"}, {"loan_leverage_k":"1", "liquidation_hard_threshold":"1.5"}, {"loan_enabled":"false"}])
async def test_invalid_existing_config_fails_without_mutation(setup_db, values):
    for key, value in values.items():
        await _seed_config(key, value)
    with pytest.raises(ValueError):
        await auto_migrate()
    async with async_session_maker() as s:
        actual = {r.key:r.value for r in (await s.execute(select(SiteConfig))).scalars()}
    assert actual == values


@pytest.mark.asyncio
@pytest.mark.parametrize("read_only", [False, True])
async def test_invalid_config_rejects_startup_all_instances(setup_db, monkeypatch, read_only):
    await _seed_config("credit_leverage", "2")
    await _seed_config("credit_maintenance_ratio", "1.5")
    monkeypatch.setenv(credit_flags.READ_ONLY_ENV, str(read_only))
    with pytest.raises(credit_flags.CreditConfigError):
        async with async_session_maker() as s:
            await credit_flags.load_flags(s)


# ─────────────────────── 热冻结（credit_new_risk_frozen） ───────────────────────

@pytest.mark.asyncio
async def test_hot_freeze_is_read_at_runtime_bypassing_ttl(setup_db):
    """风险检查消费的值必须运行期热读：直接改 DB 后（不清 site_config 缓存）立即生效。"""
    await auto_migrate()
    credit_flags.set_flags(credit_flags.CreditFlags())
    assert credit_flags.new_risk_frozen() is False

    async with async_session_maker() as s:
        assert await credit_flags.refresh_new_risk_frozen(s) is False

    # 直接改 DB，且**故意不清** site_config 的 60s TTL 缓存
    async with async_session_maker() as s:
        async with s.begin():
            row = (await s.execute(
                select(SiteConfig).where(SiteConfig.key == "credit_new_risk_frozen")
            )).scalars().one()
            row.value = "true"
            s.add(row)
    async with async_session_maker() as s:
        assert await credit_flags.refresh_new_risk_frozen(s) is True
    assert credit_flags.new_risk_frozen() is True

    # 解冻同样热生效
    async with async_session_maker() as s:
        async with s.begin():
            row = (await s.execute(
                select(SiteConfig).where(SiteConfig.key == "credit_new_risk_frozen")
            )).scalars().one()
            row.value = "false"
            s.add(row)
    async with async_session_maker() as s:
        assert await credit_flags.refresh_new_risk_frozen(s) is False
    assert credit_flags.new_risk_frozen() is False


def test_new_risk_frozen_falls_back_to_process_snapshot_and_override():
    credit_flags.set_flags(credit_flags.CreditFlags(credit_new_risk_frozen=True))
    assert credit_flags.new_risk_frozen() is True
    credit_flags.set_new_risk_frozen(False)
    assert credit_flags.new_risk_frozen() is False
    credit_flags.set_new_risk_frozen(None)
    assert credit_flags.new_risk_frozen() is True
    # set_flags 会清掉热覆写，回到新快照
    credit_flags.set_flags(credit_flags.CreditFlags())
    assert credit_flags.new_risk_frozen() is False


async def _admin_headers() -> dict[str, str]:
    suffix = uuid4().hex[:8]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=f"credit_admin_{suffix}", casdoor_id=f"cd_{suffix}",
                     cash=Decimal("0"), is_superuser=True)
            s.add(u)
            await s.flush()
            uid = u.id
    return {"Authorization": f"Bearer {create_access_token(uid)}"}


async def _seed_credit_keys(**overrides: str) -> None:
    base = {
        "credit_new_risk_frozen": ("false", "bool"),
        "credit_risk_retry_limit": ("3", "int"),
        "credit_leverage": ("2.0", "decimal"),
        "credit_maintenance_ratio": ("0.2", "decimal"),
    }
    async with async_session_maker() as s:
        async with s.begin():
            for key, (value, value_type) in base.items():
                s.add(SiteConfig(key=key, value=overrides.get(key, value), value_type=value_type))


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["loan_daily_rate", "sell_fee_rate"])
async def test_api_explains_runtime_rate_rejection_without_changing_value(client, key):
    await _seed_credit_keys()
    async with async_session_maker() as s:
        s.add(SiteConfig(key=key, value="0.01", value_type="decimal"))
        await s.commit()
        await credit_flags.load_flags(s)
    headers = await _admin_headers()
    response = await client.put(f"/api/v1/admin/site-config/{key}",
                                json={"value": "0.02"}, headers=headers)
    assert response.status_code == 400
    assert "停写维护" in response.json()["detail"]
    async with async_session_maker() as s:
        value = (await s.execute(select(SiteConfig.value).where(SiteConfig.key == key))).scalar_one()
    assert value == "0.01"


@pytest.mark.asyncio
async def test_api_cross_validates_credit_leverage_and_maintenance(client):
    await _seed_credit_keys(credit_leverage="20", credit_maintenance_ratio="0.04")
    site_config_service.clear_cache()
    headers = await _admin_headers()

    # maintenance 0.04 对 leverage 3 合法（R_initial=0.5）→ 200
    r = await client.put("/api/v1/admin/site-config/credit_leverage",
                         json={"value": "3"}, headers=headers)
    assert r.status_code == 200, r.text

    # 0.9 >= R_initial(3)=0.5 → 400
    r = await client.put("/api/v1/admin/site-config/credit_maintenance_ratio",
                         json={"value": "0.9"}, headers=headers)
    assert r.status_code == 400
    assert "R_initial" in r.json()["detail"]

    # 50 倍与旧维持率 0.04 不兼容；先降低维持率才能提高杠杆。
    r = await client.put("/api/v1/admin/site-config/credit_leverage",
                         json={"value": "50"}, headers=headers)
    assert r.status_code == 400
    r = await client.put("/api/v1/admin/site-config/credit_maintenance_ratio",
                         json={"value": "0.01"}, headers=headers)
    assert r.status_code == 200, r.text
    r = await client.put("/api/v1/admin/site-config/credit_leverage",
                         json={"value": "50"}, headers=headers)
    assert r.status_code == 200, r.text
    assert await _config_value("credit_leverage") == "50"

    # 超过 50 倍仍拒绝。
    r = await client.put("/api/v1/admin/site-config/credit_leverage",
                         json={"value": "51"}, headers=headers)
    assert r.status_code == 400
    r = await client.put("/api/v1/admin/site-config/credit_risk_retry_limit",
                         json={"value": "0"}, headers=headers)
    assert r.status_code == 400
