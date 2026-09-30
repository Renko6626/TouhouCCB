"""WP3 main.py 启动隔离：只读/非 owner 跳过全部启动写；enabled 无锁拒绝启动。

覆盖计划 WP3 "main.py 所有权必须覆盖 init_db/auto_migrate/seed/resync/writer/flusher/
全部调度器"与"只读实例在任何写之前就跳过"。
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from app import main
from app.services import candle_flusher, market_writer, tick_broadcaster
from app.services.credit import flags as credit_flags
from app.services.credit import ownership as ownership_module

pytestmark = pytest.mark.asyncio


class FakeOwnership:
    def __init__(self, owner: bool, events: list[str]):
        self.owner = owner
        self.events = events
        self.reason = "ok" if owner else "advisory_lock_held_by_other_instance"
        self.released = False
        self.read_only_marked = False

    async def acquire(self, **kwargs):
        self.events.append("acquire")
        return self.owner

    def mark_read_only(self):
        self.events.append("mark_read_only")
        self.read_only_marked = True

    async def release(self):
        self.events.append("release")
        self.released = True


class FakeWriter:
    def __init__(self, events, name):
        self.events = events
        self.name = name

    async def start(self):
        self.events.append(f"{self.name}.start")

    async def stop(self):
        self.events.append(f"{self.name}.stop")


@pytest.fixture
def startup(monkeypatch):
    """把所有启动副作用换成事件记录，返回 (events, patches setter)。"""
    events: list[str] = []
    credit_flags.clear_flags()

    async def _init_db():
        events.append("init_db")

    async def _auto_migrate():
        events.append("auto_migrate")

    async def _resync(*args, **kwargs):
        events.append("resync")

    def _setup_admin(app, engine):
        events.append("setup_admin")

    def _starter(name):
        async def _fn():
            events.append(f"start:{name}")
        return _fn

    def _stopper(name):
        async def _fn():
            events.append(f"stop:{name}")
        return _fn

    monkeypatch.setattr(main, "init_db", _init_db)
    monkeypatch.setattr(main, "auto_migrate", _auto_migrate)
    monkeypatch.setattr(main, "_resync_recent_candles", _resync)
    monkeypatch.setattr(main, "setup_admin", _setup_admin)
    for name in ("loan", "liquidation", "bot_detection", "pve", "fx"):
        monkeypatch.setattr(main, f"start_{name}_scheduler", _starter(name))
        monkeypatch.setattr(main, f"stop_{name}_scheduler", _stopper(name))
    monkeypatch.setattr(main, "start_fx_publisher", _starter("fx_publisher"))
    monkeypatch.setattr(main, "stop_fx_publisher", _stopper("fx_publisher"))
    monkeypatch.setattr(market_writer, "WRITER", FakeWriter(events, "WRITER"))
    monkeypatch.setattr(candle_flusher, "CANDLE_FLUSHER", FakeWriter(events, "FLUSHER"))
    monkeypatch.setattr(tick_broadcaster, "TICK_BROADCASTER", FakeWriter(events, "TICK"))
    monkeypatch.delenv(credit_flags.READ_ONLY_ENV, raising=False)
    yield events
    credit_flags.clear_flags()


def _install_ownership(monkeypatch, owner: bool, events: list[str]) -> FakeOwnership:
    fake = FakeOwnership(owner=owner, events=events)
    monkeypatch.setattr(ownership_module, "OWNERSHIP", fake)
    return fake


def _install_flags(monkeypatch, raw: dict, *, error: Exception | None = None):
    async def _load(session):
        if error is not None:
            raise error
        credit_flags.set_flags(
            credit_flags.parse_flags(raw, read_only=credit_flags.read_only_from_env())
        )
        return credit_flags.get_flags()

    monkeypatch.setattr(credit_flags, "load_flags", _load)


async def _run_lifespan():
    async with main.lifespan(main.app):
        pass


async def test_read_only_skips_all_startup_writes(startup, monkeypatch):
    monkeypatch.setenv(credit_flags.READ_ONLY_ENV, "true")
    own = _install_ownership(monkeypatch, owner=True, events=startup)
    _install_flags(monkeypatch, {})

    await _run_lifespan()

    assert own.read_only_marked is True
    assert "acquire" not in startup              # 只读实例不取锁
    for forbidden in ("init_db", "auto_migrate", "resync", "setup_admin",
                      "start:loan", "start:liquidation", "start:bot_detection",
                      "start:pve", "start:fx", "start:fx_publisher",
                      "WRITER.start", "FLUSHER.start"):
        assert forbidden not in startup, f"read-only 实例不应执行 {forbidden}"
    assert "TICK.start" in startup               # 广播只读，允许
    assert main.app.state.credit_writes_enabled is False
    assert own.released is True


async def test_owner_runs_startup_writes_after_acquiring_lock(startup, monkeypatch):
    from app.core.database import async_session_maker
    from app.models.base import SiteConfig

    async with async_session_maker() as s:
        async with s.begin():
            s.add(SiteConfig(key="single_writer_enabled", value="true", value_type="bool"))
    own = _install_ownership(monkeypatch, owner=True, events=startup)
    _install_flags(monkeypatch, {})

    await _run_lifespan()

    assert startup.index("acquire") < startup.index("init_db")
    assert "acquire" in own.events
    assert startup.index("init_db") < startup.index("auto_migrate")
    assert startup.index("auto_migrate") < startup.index("resync")
    assert "setup_admin" in startup
    assert startup.index("setup_admin") < startup.index("resync")
    for expected in ("start:loan", "start:liquidation", "start:bot_detection",
                     "start:pve", "start:fx", "start:fx_publisher", "WRITER.start", "FLUSHER.start",
                     "TICK.start"):
        assert expected in startup, f"owner 实例应执行 {expected}"
    assert main.app.state.credit_writes_enabled is True
    assert startup.index("start:fx") < startup.index("start:fx_publisher")
    assert startup.index("stop:fx") < startup.index("stop:fx_publisher") < startup.index("TICK.stop")
    assert own.released is True


async def test_non_owner_with_unified_credit_enabled_fails_startup(startup, monkeypatch):
    own = _install_ownership(monkeypatch, owner=False, events=startup)
    _install_flags(monkeypatch, {
        "unified_credit_enabled": "true",
        "credit_leverage": "20",
        "credit_maintenance_ratio": "0.04",
    })

    with pytest.raises(RuntimeError, match="第二写实例"):
        await _run_lifespan()

    assert "init_db" not in startup and "auto_migrate" not in startup
    assert "resync" not in startup
    assert not any(e.startswith("start:") for e in startup)
    assert own.released is True  # 启动失败也要释放所有权


async def test_unified_credit_starts_writer_without_legacy_writer_flag(startup, monkeypatch):
    _install_ownership(monkeypatch, owner=True, events=startup)
    _install_flags(monkeypatch, {"unified_credit_enabled": "true", "credit_leverage": "20",
                               "credit_maintenance_ratio": "0.04"})
    try:
        await _run_lifespan()
        assert "WRITER.start" in startup
        assert "FLUSHER.start" in startup
    finally:
        main._configure_admin_economic_writes(False)


async def test_non_owner_without_unified_credit_runs_without_writes(startup, monkeypatch):
    own = _install_ownership(monkeypatch, owner=False, events=startup)
    _install_flags(monkeypatch, {"unified_credit_enabled": "false"})

    await _run_lifespan()

    for forbidden in ("init_db", "auto_migrate", "resync", "setup_admin",
                      "start:loan", "start:liquidation", "start:bot_detection",
                      "start:pve", "start:fx", "start:fx_publisher", "WRITER.start", "FLUSHER.start"):
        assert forbidden not in startup, f"非 owner 不应执行 {forbidden}"
    assert "TICK.start" in startup
    assert main.app.state.credit_writes_enabled is False
    assert own.released is True


async def test_invalid_credit_config_fails_startup_and_releases_lock(startup, monkeypatch):
    own = _install_ownership(monkeypatch, owner=True, events=startup)

    async def _raise(session):
        raise credit_flags.CreditConfigError("invalid_thresholds: boom")

    monkeypatch.setattr(credit_flags, "load_flags", _raise)

    with pytest.raises(credit_flags.CreditConfigError):
        await _run_lifespan()

    assert "resync" not in startup
    assert own.released is True


async def test_non_owner_cannot_read_flags_fails_startup(startup, monkeypatch):
    own = _install_ownership(monkeypatch, owner=False, events=startup)

    async def _boom(session):
        raise RuntimeError("db down")

    monkeypatch.setattr(credit_flags, "load_flags", _boom)

    with pytest.raises(RuntimeError, match="非 owner 实例无法读取"):
        await _run_lifespan()
    assert own.released is True


async def test_readonly_starts_even_with_unified_credit_enabled(startup, monkeypatch):
    """reviewer blocker 5：只读实例不取写锁，运营已开统一信贷也必须能启动。"""
    monkeypatch.setenv(credit_flags.READ_ONLY_ENV, "true")
    own = _install_ownership(monkeypatch, owner=True, events=startup)
    _install_flags(monkeypatch, {
        "unified_credit_enabled": "true",
        "credit_leverage": "20",
        "credit_maintenance_ratio": "0.04",
    })

    await _run_lifespan()          # 不得抛 RuntimeError

    assert "acquire" not in startup
    assert "init_db" not in startup and "auto_migrate" not in startup
    assert "setup_admin" not in startup
    assert not any(e.startswith("start:") for e in startup)
    assert main.app.state.credit_writes_enabled is False
    assert own.read_only_marked is True and own.released is True


async def test_start_failure_still_runs_cleanup_and_releases_ownership(startup, monkeypatch):
    """reviewer blocker 6：启动中途失败也要停已启动资源 + 释放所有权。"""
    own = _install_ownership(monkeypatch, owner=True, events=startup)
    _install_flags(monkeypatch, {})

    async def _boom():
        raise RuntimeError("scheduler start failed")

    monkeypatch.setattr(main, "start_liquidation_scheduler", _boom)

    with pytest.raises(RuntimeError, match="scheduler start failed"):
        await _run_lifespan()

    assert "init_db" in startup and "auto_migrate" in startup
    assert "start:loan" in startup                 # loan 已启动
    assert "start:liquidation" not in startup      # 失败的没启动
    for stopped in ("stop:loan", "stop:liquidation", "stop:bot_detection",
                    "stop:pve", "stop:fx", "stop:fx_publisher", "WRITER.stop", "TICK.stop", "FLUSHER.stop"):
        assert stopped in startup, f"启动失败清理缺少 {stopped}"
    assert own.released is True


async def test_shutdown_step_failure_does_not_block_cleanup(startup, monkeypatch):
    """reviewer blocker 6：shutdown 单步异常不得阻断其余清理与所有权释放。"""
    own = _install_ownership(monkeypatch, owner=True, events=startup)
    _install_flags(monkeypatch, {})

    async def _boom():
        raise RuntimeError("pve stop failed")

    monkeypatch.setattr(main, "stop_pve_scheduler", _boom)

    await _run_lifespan()

    for stopped in ("stop:fx", "stop:fx_publisher", "stop:bot_detection", "stop:liquidation", "stop:loan",
                    "WRITER.stop", "TICK.stop", "FLUSHER.stop"):
        assert stopped in startup, f"单步失败后缺少 {stopped}"
    assert own.released is True


async def test_unified_sqladmin_economic_views_are_read_only():
    from app.core.admin import UserAdmin, MarketAdmin, OutcomeAdmin, PositionAdmin, TransactionAdmin
    views = (UserAdmin, MarketAdmin, OutcomeAdmin, PositionAdmin, TransactionAdmin)
    try:
        main._configure_admin_economic_writes(True)
        assert all(not view.can_create and not view.can_edit and not view.can_delete for view in views)
    finally:
        main._configure_admin_economic_writes(False)
    assert all(view.can_create and view.can_edit and view.can_delete for view in views)


async def _persist_short(*, principal="0", interest="1", restricted="0"):
    from datetime import datetime, timezone
    from decimal import Decimal
    from app.core.database import async_session_maker
    from app.models.base import SiteConfig, User
    from app.models.fx import FxPair, FxShortPosition
    async with async_session_maker() as db:
        async with db.begin():
            db.add(SiteConfig(key="unified_credit_enabled", value="false", value_type="bool"))
            user = User(username="gate-short", casdoor_id="gate-short", cash=Decimal("10"))
            pair = FxPair(currency_code="GATE", currency_name="gate")
            db.add_all([user, pair])
            await db.flush()
            db.add(FxShortPosition(user_id=user.id, pair_id=pair.id,
                principal_foreign=Decimal(principal), interest_foreign=Decimal(interest),
                restricted_gold=Decimal(restricted), interest_last_accrued_at=datetime.now(timezone.utc)))
            return user.id


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("obligation", [{"interest": "1"}, {"principal": "1", "interest": "0", "restricted": "2"}])
async def test_live_foreign_obligation_refuses_legacy_startup(startup, monkeypatch, read_only, obligation):
    await _persist_short(**obligation)
    own = _install_ownership(monkeypatch, owner=True, events=startup)
    if read_only:
        monkeypatch.setenv(credit_flags.READ_ONLY_ENV, "true")
    with pytest.raises(credit_flags.CreditConfigError, match="fx_short"):
        await _run_lifespan()
    assert "setup_admin" not in startup and "resync" not in startup
    assert not any(e.startswith("start:") or e.endswith(".start") for e in startup)
    assert own.released


async def test_disable_unified_with_short_preserves_config_and_audit(startup):
    from fastapi import HTTPException
    from sqlalchemy import select, func
    from app.core.database import async_session_maker
    from app.models.base import User, SiteConfig
    from app.models.audit import AuditEvent
    from app.schemas.loan import SiteConfigUpdate
    from app.api.v1.site_config import update_config
    user_id = await _persist_short()
    async with async_session_maker() as db:
        row = (await db.execute(select(SiteConfig).where(SiteConfig.key == "unified_credit_enabled"))).scalar_one()
        row.value = "true"
        await db.commit()
        before = (await db.execute(select(func.count()).select_from(AuditEvent))).scalar_one()
        admin = await db.get(User, user_id)
        with pytest.raises(HTTPException) as rejected:
            await update_config("unified_credit_enabled", SiteConfigUpdate(value="false"), admin, db)
        assert rejected.value.status_code == 400
        await db.refresh(row)
        assert row.value == "true"
        assert (await db.execute(select(func.count()).select_from(AuditEvent))).scalar_one() == before
        await update_config("fx_short_enabled", SiteConfigUpdate(value="false"), admin, db)
