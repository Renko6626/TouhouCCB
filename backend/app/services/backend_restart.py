"""Self-restart for the single PID-1 uvicorn managed by Docker.

Only the serving process is signalled; no host command or Docker socket access.
The hidden SiteConfig timestamp survives a restart and uses existing config audit.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import signal
import time
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.base import SiteConfig
from app.services import audit_service
from app.services.credit.ownership import OWNERSHIP

INSTANCE_ID = uuid.uuid4().hex
LAST_RESTART_KEY = "backend_last_restart_at"
COOLDOWN_SECONDS = 60
SIGNAL_DELAY_SECONDS = 1
_started_at = time.monotonic()
_pending = False
_lock = asyncio.Lock()
logger = logging.getLogger("thccb.backend_restart")


def _disabled_reason() -> str | None:
    if not settings.ADMIN_RESTART_ENABLED or os.getpid() != 1:
        return "当前部署未启用页面重启，请使用服务器重启命令。"
    if not OWNERSHIP.writes_enabled:
        return "当前实例没有写入权限，无法记录重启操作。"
    return None


async def _last_restart(db: AsyncSession) -> SiteConfig | None:
    return (await db.execute(select(SiteConfig).where(
        SiteConfig.key == LAST_RESTART_KEY))).scalar_one_or_none()


def _cooldown(row: SiteConfig | None) -> int:
    # Docker only monitors its restart policy after 10 seconds of uptime.
    remaining = max(0, 10 - (time.monotonic() - _started_at))
    if row:
        try:
            last = datetime.fromisoformat(row.value)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            remaining = max(remaining, COOLDOWN_SECONDS - (
                datetime.now(timezone.utc) - last).total_seconds())
        except ValueError as exc:
            raise HTTPException(503, "重启记录异常，请联系服务器管理员。") from exc
    return math.ceil(max(0, remaining))


async def status(db: AsyncSession) -> dict:
    # A successful response verifies DB access as well as process availability.
    row = await _last_restart(db)
    reason = _disabled_reason()
    return {
        "instance_id": INSTANCE_ID,
        "restart_enabled": reason is None,
        "restart_pending": _pending,
        "cooldown_seconds": _cooldown(row),
        "reason": reason,
    }


async def request_restart(db: AsyncSession, admin_id: int, instance_id: str) -> dict:
    global _pending
    async with _lock:
        reason = _disabled_reason()
        if reason:
            raise HTTPException(503, reason)
        if instance_id != INSTANCE_ID:
            raise HTTPException(409, "后端实例已变化，请刷新状态后重试。")
        if _pending:
            raise HTTPException(409, "后端正在重启，请等待恢复。")
        row = await _last_restart(db)
        cooldown = _cooldown(row)
        if cooldown:
            raise HTTPException(429, f"请在 {cooldown} 秒后再重启。",
                                headers={"Retry-After": str(cooldown)})
        now = datetime.now(timezone.utc)
        old_value = row.value if row else None
        if row is None:
            row = SiteConfig(key=LAST_RESTART_KEY, value="", value_type="string")
        row.value = now.isoformat()
        row.updated_at = now
        row.updated_by = admin_id
        db.add(row)
        audit_service.record(db, "config_set", operator_user_id=admin_id, payload={
            "key": LAST_RESTART_KEY, "old": old_value, "new": row.value,
            "value_type": "string", "action": "backend_restart",
            "instance_id": INSTANCE_ID,
        })
        OWNERSHIP.require_writes()
        await db.commit()  # A failed audit must never terminate the process.
        _pending = True
        logger.warning("ADMIN_BACKEND_RESTART admin_id=%s instance_id=%s", admin_id, INSTANCE_ID)
        return {"instance_id": INSTANCE_ID, "status": "accepted"}


async def stop_process() -> None:
    # BackgroundTasks starts after the HTTP response is sent. Existing uvicorn
    # graceful shutdown drains requests and runs lifespan resource cleanup.
    await asyncio.sleep(SIGNAL_DELAY_SECONDS)
    os.kill(os.getpid(), signal.SIGTERM)
