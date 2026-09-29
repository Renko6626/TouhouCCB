"""WP1：统一信贷配置键、迁移种子、flags 缓存与 admin 交叉校验（计划 §3.4 / F6 / F7）。"""
import os
import sys
from decimal import Decimal
from uuid import uuid4

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.audit import AuditEvent
from app.models.base import SiteConfig, User
from app.services import site_config as site_config_service
from app.services.credit import flags as credit_flags
from app.services.loan_migrate import auto_migrate, seed_credit_risk_configs


@pytest_asyncio.fixture(autouse=True)
async def _reset_flags():
    credit_flags.clear_flags()
    yield
    credit_flags.clear_flags()


# ─────────────────────────── parse_flags（纯函数） ───────────────────────────

def test_parse_flags_defaults_to_legacy_behavior():
    flags = credit_flags.parse_flags({})
    assert flags.unified_credit_enabled is False
    assert flags.credit_new_risk_frozen is False
    assert flags.credit_risk_retry_limit == 3
    assert flags.thresholds is None
    assert flags.disabled_reason is None


def test_parse_flags_valid_config_enables_engine():
    flags = credit_flags.parse_flags({
        "unified_credit_enabled": "true",
        "credit_new_risk_frozen": "true",
        "credit_leverage": "20",
        "credit_maintenance_ratio": "0.04",
        "credit_risk_retry_limit": "5",
    })
    assert flags.unified_credit_enabled is True
    assert flags.credit_new_risk_frozen is True
    assert flags.credit_risk_retry_limit == 5
    assert flags.risk_engine_ready is True
    assert flags.thresholds.r_initial == Decimal(1) / Decimal(19)
    assert flags.disabled_reason is None


@pytest.mark.parametrize("raw, reason_part", [
    ({"unified_credit_enabled": "true"}, "missing_credit_leverage"),
    ({"unified_credit_enabled": "true", "credit_leverage": "20"},
     "missing_credit_maintenance_ratio"),
    ({"unified_credit_enabled": "true", "credit_leverage": "21",
      "credit_maintenance_ratio": "0.04"}, "leverage 超过上限"),
    ({"unified_credit_enabled": "true", "credit_leverage": "1",
      "credit_maintenance_ratio": "0.04"}, "leverage 必须 > 1"),
    ({"unified_credit_enabled": "true", "credit_leverage": "20",
      "credit_maintenance_ratio": "0.06"}, "必须 < R_initial"),
    ({"unified_credit_enabled": "true", "credit_leverage": "20",
      "credit_maintenance_ratio": "0"}, "maintenance 必须 > 0"),
    ({"unified_credit_enabled": "true", "credit_leverage": "abc",
      "credit_maintenance_ratio": "0.04"}, "missing_credit_leverage"),
])
def test_parse_flags_refuses_to_enable_with_bad_config(raw, reason_part):
    flags = credit_flags.parse_flags(raw)
    assert flags.unified_credit_enabled is False
    assert flags.disabled_reason is not None
    assert reason_part in flags.disabled_reason


def test_parse_flags_retry_limit_falls_back_to_default():
    assert credit_flags.parse_flags({"credit_risk_retry_limit": "abc"}).credit_risk_retry_limit == 3
    assert credit_flags.parse_flags({"credit_risk_retry_limit": "0"}).credit_risk_retry_limit == 3
    assert credit_flags.parse_flags({"credit_risk_retry_limit": "999"}).credit_risk_retry_limit == 3
    assert credit_flags.parse_flags({"credit_risk_retry_limit": "9"}).credit_risk_retry_limit == 9


def test_seeded_thresholds_are_unavailable_while_unified_credit_is_off():
    flags = credit_flags.parse_flags({
        "unified_credit_enabled": "false",
        "credit_leverage": "2",
        "credit_maintenance_ratio": "0.2",
    })
    assert flags.thresholds is None
    assert flags.risk_engine_ready is False


def test_read_only_hook_is_env_driven_and_gates_write_schedulers(monkeypatch):
    monkeypatch.delenv(credit_flags.READ_ONLY_ENV, raising=False)
    assert credit_flags.read_only_from_env() is False
    assert credit_flags.write_schedulers_enabled() is True

    credit_flags.set_flags(credit_flags.parse_flags({}, read_only=True))
    assert credit_flags.write_schedulers_enabled() is False
    credit_flags.clear_flags()
    assert credit_flags.write_schedulers_enabled() is True

    monkeypatch.setenv(credit_flags.READ_ONLY_ENV, "true")
    assert credit_flags.read_only_from_env() is True


# ─────────────────────────── 迁移种子（DB） ───────────────────────────

async def _seed_config(key: str, value: str, value_type: str = "decimal") -> None:
    async with async_session_maker() as s:
        async with s.begin():
            s.add(SiteConfig(key=key, value=value, value_type=value_type))


async def _config_value(key: str) -> str | None:
    async with async_session_maker() as s:
        row = (await s.execute(select(SiteConfig).where(SiteConfig.key == key))).scalars().first()
        return None if row is None else row.value


async def _audit_payloads(event_type: str = "config_set") -> list[dict]:
    async with async_session_maker() as s:
        rows = (await s.execute(
            select(AuditEvent).where(AuditEvent.event_type == event_type).order_by(AuditEvent.id)
        )).scalars().all()
        return [dict(r.payload or {}) for r in rows]


@pytest.mark.asyncio
async def test_auto_migrate_seeds_credit_configs_with_audit(setup_db):
    await auto_migrate()

    assert await _config_value("unified_credit_enabled") == "false"
    assert await _config_value("credit_new_risk_frozen") == "false"
    assert await _config_value("credit_risk_retry_limit") == "3"
    # F7：k=1.0 → credit_leverage=2.0；F6：hard 0.2 < R_initial(2)=1 → 沿用
    assert await _config_value("credit_leverage") == "2.0"
    assert await _config_value("credit_maintenance_ratio") == "0.2"

    migrated = {
        p["key"]: p for p in await _audit_payloads()
        if p.get("source") == "credit_migration"
    }
    assert set(migrated) == {"credit_leverage", "credit_maintenance_ratio"}
    assert migrated["credit_leverage"]["new"] == "2.0"
    assert migrated["credit_leverage"]["old"] is None

    # 幂等：再跑一次不重复写、不重复审计
    assert await seed_credit_risk_configs() == []
    assert len([p for p in await _audit_payloads() if p.get("source") == "credit_migration"]) == 2


@pytest.mark.asyncio
async def test_auto_migrate_refuses_legacy_k_above_19(setup_db):
    """Controller 裁定：>19 说的是旧 k（k+1 <= 20）；k=20 必须拒绝并保持未 seed。"""
    await _seed_config("loan_leverage_k", "20")
    await auto_migrate()
    assert await _config_value("credit_leverage") is None
    # hard 阈值本身合法（auto_migrate 种了 0.2），但没有有效 leverage 就不派生维持率
    assert await _config_value("liquidation_hard_threshold") == "0.2"
    assert await _config_value("credit_maintenance_ratio") is None
    assert await seed_credit_risk_configs() == []


@pytest.mark.asyncio
async def test_auto_migrate_does_not_seed_invalid_hard_threshold(setup_db):
    """迁移期只有 0 < hard < R_initial(credit_leverage) 才沿用；否则不写（等运营设 0.04）。"""
    await _seed_config("loan_leverage_k", "1")
    await _seed_config("liquidation_hard_threshold", "1.5")   # R_initial(2)=1
    await auto_migrate()
    assert await _config_value("credit_leverage") == "2"
    assert await _config_value("credit_maintenance_ratio") is None


@pytest.mark.asyncio
async def test_auto_migrate_never_overwrites_existing_credit_config(setup_db):
    await _seed_config("credit_leverage", "20")
    await _seed_config("credit_maintenance_ratio", "0.04")
    await _seed_config("loan_leverage_k", "10")
    await auto_migrate()
    assert await _config_value("credit_leverage") == "20"
    assert await _config_value("credit_maintenance_ratio") == "0.04"


@pytest.mark.asyncio
async def test_load_flags_reads_db_and_defaults_off(setup_db):
    await auto_migrate()
    site_config_service.clear_cache()
    async with async_session_maker() as s:
        flags = await credit_flags.load_flags(s)
    assert flags.unified_credit_enabled is False
    assert flags.credit_leverage == Decimal("2.0")
    assert flags.credit_maintenance_ratio == Decimal("0.2")
    assert credit_flags.get_flags() is flags   # 进程级缓存

    # 打开开关 + 合法门槛 → 重启后（重新 load）生效
    async with async_session_maker() as s:
        async with s.begin():
            for key, value in (
                ("unified_credit_enabled", "true"),
                ("credit_leverage", "20"),
                ("credit_maintenance_ratio", "0.04"),
            ):
                row = (await s.execute(select(SiteConfig).where(SiteConfig.key == key))).scalars().one()
                row.value = value
                s.add(row)
    site_config_service.clear_cache()
    async with async_session_maker() as s:
        flags = await credit_flags.load_flags(s)
    assert flags.unified_credit_enabled is True
    assert flags.thresholds.r_maintenance == Decimal("0.04")


@pytest.mark.asyncio
async def test_load_flags_refuses_enable_with_bad_db_config(setup_db, caplog):
    await auto_migrate()
    async with async_session_maker() as s:
        async with s.begin():
            for key, value in (
                ("unified_credit_enabled", "true"),
                ("credit_maintenance_ratio", "1.5"),   # >= R_initial(2)=1 → 非法
            ):
                row = (await s.execute(
                    select(SiteConfig).where(SiteConfig.key == key)
                )).scalars().one()
                row.value = value
                s.add(row)
    site_config_service.clear_cache()
    with caplog.at_level("CRITICAL", logger="thccb.credit.flags"):
        async with async_session_maker() as s:
            flags = await credit_flags.load_flags(s)
    assert flags.unified_credit_enabled is False
    assert flags.disabled_reason is not None
    assert any(r.levelname == "CRITICAL" for r in caplog.records)


# ─────────────────────────── admin API 交叉校验 ───────────────────────────

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
        "unified_credit_enabled": ("false", "bool"),
        "credit_new_risk_frozen": ("false", "bool"),
        "credit_risk_retry_limit": ("3", "int"),
        "credit_leverage": ("2.0", "decimal"),
        "credit_maintenance_ratio": ("0.2", "decimal"),
        "loan_leverage_k": ("1.0", "decimal"),
    }
    async with async_session_maker() as s:
        async with s.begin():
            for key, (value, value_type) in base.items():
                s.add(SiteConfig(key=key, value=overrides.get(key, value), value_type=value_type))


@pytest.mark.asyncio
async def test_api_rejects_enable_without_valid_credit_config(client):
    await _seed_credit_keys(credit_leverage="2.0", credit_maintenance_ratio="1.5")
    site_config_service.clear_cache()

    headers = await _admin_headers()
    r = await client.put("/api/v1/admin/site-config/unified_credit_enabled",
                         json={"value": "true"}, headers=headers)
    assert r.status_code == 400
    assert "拒绝启用" in r.json()["detail"]


@pytest.mark.asyncio
async def test_api_allows_enable_with_valid_credit_config(client):
    await _seed_credit_keys(credit_leverage="20", credit_maintenance_ratio="0.04")
    site_config_service.clear_cache()
    headers = await _admin_headers()
    r = await client.put("/api/v1/admin/site-config/unified_credit_enabled",
                         json={"value": "true"}, headers=headers)
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_api_rejects_credit_edit_when_migration_not_seeded(client):
    """迁移未派生（行不存在）时给 400，而不是让 set_value 抛 500。"""
    await _seed_credit_keys()
    async with async_session_maker() as s:
        async with s.begin():
            row = (await s.execute(
                select(SiteConfig).where(SiteConfig.key == "credit_leverage")
            )).scalars().one()
            await s.delete(row)
    site_config_service.clear_cache()
    headers = await _admin_headers()
    r = await client.put("/api/v1/admin/site-config/credit_leverage",
                         json={"value": "3"}, headers=headers)
    assert r.status_code == 400
    assert "尚未由迁移 seed" in r.json()["detail"]


@pytest.mark.asyncio
async def test_api_rejects_legacy_leverage_edit_while_unified_on(client):
    """Controller 裁定：只在该开关 ON 时禁止冲突的旧 k 写入（OFF 时保持旧行为）。"""
    await _seed_credit_keys(unified_credit_enabled="true",
                            credit_leverage="20", credit_maintenance_ratio="0.04")
    site_config_service.clear_cache()
    headers = await _admin_headers()

    r = await client.put("/api/v1/admin/site-config/loan_leverage_k",
                         json={"value": "2"}, headers=headers)
    assert r.status_code == 400
    assert "loan_leverage_k" in r.json()["detail"]


@pytest.mark.asyncio
async def test_api_allows_legacy_leverage_edit_while_unified_off(client):
    await _seed_credit_keys(unified_credit_enabled="false")
    site_config_service.clear_cache()
    headers = await _admin_headers()
    r = await client.put("/api/v1/admin/site-config/loan_leverage_k",
                         json={"value": "2"}, headers=headers)
    assert r.status_code == 200, r.text


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

    # 越界值仍按 §3.4 区间拒绝
    r = await client.put("/api/v1/admin/site-config/credit_leverage",
                         json={"value": "21"}, headers=headers)
    assert r.status_code == 400
    r = await client.put("/api/v1/admin/site-config/credit_risk_retry_limit",
                         json={"value": "0"}, headers=headers)
    assert r.status_code == 400
