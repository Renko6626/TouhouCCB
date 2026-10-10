"""强制平仓定时扫描。仿 loan_sweep 模式，每 N 秒扫一次 debt>0 用户。

- run_liquidation_sweep_once()：扫一次，也给 admin run-now 复用
- start_scheduler() / stop_scheduler()：FastAPI lifespan 调
- reschedule(interval_sec)：管理员改 site_config 后调用

保留共享死锁识别，统一执行器负责账户级候选发现和恢复。
"""
from __future__ import annotations
import asyncio
import logging
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.exc import DBAPIError

from app.core.database import async_session_maker
from app.services import site_config


logger = logging.getLogger("thccb.liquidation_sweep")

_scheduler: Optional[AsyncIOScheduler] = None
_JOB_ID = "liquidation_sweep_tick"


def _is_deadlock_error(exc: Exception) -> bool:
    """识别 Postgres / SQLite deadlock 类异常。"""
    if isinstance(exc, DBAPIError):
        # Postgres: SQLSTATE 40P01 (deadlock_detected)
        # SQLite: "database is locked" or OperationalError
        msg = str(exc).lower()
        return ("deadlock" in msg) or ("40p01" in msg)
    return False




_SWEEP_LOCK = asyncio.Lock()


async def run_liquidation_sweep_once(trigger_source: str = "scheduler") -> dict:
    """扫一次全体 debt>0 用户。给 scheduler + admin run-now 共用。

    trigger_source: "scheduler"（定时 cron 触发）或 "admin_manual"（管理员手动触发）。

    进程内互斥：APS 的 max_instances=1 只管 cron job 自身，管理员 run-now 直接调本函数
    会与 cron 重叠——writer 路径阶段 A 放锁后两次扫描都能过复检，partial 模式会各卖一份
    （核心审计 #4）。重叠时后到者直接返回 skipped，不排队。
    """
    if _SWEEP_LOCK.locked():
        logger.info("liquidation sweep skipped: another sweep in progress (trigger=%s)", trigger_source)
        return {"skipped": "sweep_in_progress"}
    async with _SWEEP_LOCK:
        from app.services.credit.ownership import OWNERSHIP
        OWNERSHIP.require_writes()
        from app.services.credit.sweep import run_sweep
        return await run_sweep(trigger_source)




async def _tick_safe():
    try:
        await run_liquidation_sweep_once()
    except Exception:
        logger.exception("liquidation_sweep_tick_failed")


async def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    async with async_session_maker() as session:
        try:
            interval = await site_config.get_int(
                session, "liquidation_sweep_interval_sec"
            )
        except Exception:
            interval = 600
    interval = max(5, min(7200, interval))
    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(
        _tick_safe, "interval", seconds=interval,
        id=_JOB_ID, max_instances=1, coalesce=True,
    )
    _scheduler.start()
    logger.info("liquidation_sweep_started", extra={"interval_sec": interval})


async def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


async def reschedule(interval_sec: int) -> None:
    global _scheduler
    if _scheduler is None:
        return
    interval = max(5, min(7200, interval_sec))
    _scheduler.reschedule_job(
        _JOB_ID, trigger="interval", seconds=interval,
    )
    logger.info("liquidation_sweep_rescheduled", extra={"interval_sec": interval})
