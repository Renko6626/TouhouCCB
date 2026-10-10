"""管理员重启权限及持久冷却；信号替换，绝不退出测试进程。"""
import asyncio
import signal
import time
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.audit import AuditEvent
from app.models.base import SiteConfig, User


async def _admin_headers(superuser=True):
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        user = User(username=f"restart_{suffix}", email=f"{suffix}@test.com",
                    casdoor_id=suffix, is_superuser=superuser)
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user.id, {"Authorization": f"Bearer {create_access_token(user.id)}"}


@pytest.fixture
def managed_restart(monkeypatch):
    from app.services import backend_restart
    signals = []
    monkeypatch.setattr(settings, "ADMIN_RESTART_ENABLED", True)
    monkeypatch.setattr(backend_restart, "os", SimpleNamespace(
        getpid=lambda: 1, kill=lambda pid, sig: signals.append((pid, sig))))
    monkeypatch.setattr(backend_restart, "_started_at", time.monotonic() - 20)
    monkeypatch.setattr(backend_restart, "_pending", False)
    monkeypatch.setattr(backend_restart, "SIGNAL_DELAY_SECONDS", 0)
    return signals


@pytest.mark.asyncio
async def test_system_operations_require_admin(client, setup_db):
    _, headers = await _admin_headers(superuser=False)
    for auth in ({}, headers):
        response = await client.get("/api/v1/admin/system/status", headers=auth)
        assert response.status_code in (401, 403)
        response = await client.post("/api/v1/admin/system/restart", headers=auth,
                                     json={"instance_id": "any"})
        assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_restart_disabled_outside_managed_deployment(client, setup_db, monkeypatch):
    from app.services import backend_restart
    _, headers = await _admin_headers()
    monkeypatch.setattr(settings, "ADMIN_RESTART_ENABLED", False)
    response = await client.get("/api/v1/admin/system/status", headers=headers)
    assert response.json()["restart_enabled"] is False
    response = await client.post("/api/v1/admin/system/restart", headers=headers,
                                 json={"instance_id": backend_restart.INSTANCE_ID})
    assert response.status_code == 503
    monkeypatch.setattr(settings, "ADMIN_RESTART_ENABLED", True)
    monkeypatch.setattr(backend_restart, "os", SimpleNamespace(getpid=lambda: 42))
    response = await client.post("/api/v1/admin/system/restart", headers=headers,
                                 json={"instance_id": backend_restart.INSTANCE_ID})
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_restart_persists_operator_and_cooldown(client, setup_db, managed_restart, monkeypatch):
    from app.services import backend_restart
    user_id, headers = await _admin_headers()
    before = (await client.get("/api/v1/admin/system/status", headers=headers)).json()
    response = await client.post("/api/v1/admin/system/restart", headers=headers,
                                 json={"instance_id": before["instance_id"]})
    assert response.status_code == 202, response.text
    assert response.json()["instance_id"] == before["instance_id"]
    assert managed_restart == [(1, signal.SIGTERM)]
    async with async_session_maker() as db:
        row = (await db.execute(select(SiteConfig).where(
            SiteConfig.key == backend_restart.LAST_RESTART_KEY))).scalar_one()
        events = (await db.execute(select(AuditEvent))).scalars().all()
    assert row.updated_by == user_id
    assert len(events) == 1 and events[0].operator_user_id == user_id
    assert events[0].payload["key"] == backend_restart.LAST_RESTART_KEY
    # Simulate a new process: the DB timestamp must still block another restart.
    monkeypatch.setattr(backend_restart, "_pending", False)
    status = (await client.get("/api/v1/admin/system/status", headers=headers)).json()
    assert status["cooldown_seconds"] > 0
    response = await client.post("/api/v1/admin/system/restart", headers=headers,
                                 json={"instance_id": status["instance_id"]})
    assert response.status_code == 429
    assert len(managed_restart) == 1


@pytest.mark.asyncio
async def test_stale_instance_cannot_restart_new_process(client, setup_db, managed_restart):
    _, headers = await _admin_headers()
    response = await client.post("/api/v1/admin/system/restart", headers=headers,
                                 json={"instance_id": "old-instance"})
    assert response.status_code == 409
    assert managed_restart == []


@pytest.mark.asyncio
async def test_concurrent_requests_accept_only_one_restart(client, setup_db, managed_restart):
    from app.services import backend_restart
    _, headers = await _admin_headers()
    responses = await asyncio.gather(*(
        client.post("/api/v1/admin/system/restart", headers=headers,
                    json={"instance_id": backend_restart.INSTANCE_ID}) for _ in range(2)))
    assert sorted(r.status_code for r in responses) == [202, 409]
    assert len(managed_restart) == 1


@pytest.mark.asyncio
async def test_failed_audit_never_schedules_restart(setup_db, managed_restart, monkeypatch):
    from app.services import backend_restart
    user_id, _ = await _admin_headers()
    async with async_session_maker() as db:
        async def fail_commit():
            raise RuntimeError("database unavailable")
        monkeypatch.setattr(db, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="database unavailable"):
            await backend_restart.request_restart(db, user_id, backend_restart.INSTANCE_ID)
        await db.rollback()
    async with async_session_maker() as db:
        status = await backend_restart.status(db)
        assert status["restart_pending"] is False
        assert status["cooldown_seconds"] == 0
    assert managed_restart == []
