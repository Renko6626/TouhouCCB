"""PvE 调度器 lifespan 包装——与 bot_detection 同构（APScheduler，max_instances=1）。

急停不在这里：pve_enabled 由 ENGINE.tick() 每轮开头检查（site_config 热配），
关闸后最迟一个 tick 周期内全体停手；本调度器只随进程 lifespan 起停。
"""
from __future__ import annotations

import logging
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.services.pve.engine import ENGINE

logger = logging.getLogger("thccb.pve")

_scheduler: Optional[AsyncIOScheduler] = None
_JOB_ID = "pve_tick"


async def _tick_safe():
    try:
        await ENGINE.tick()
    except Exception:
        logger.exception("pve_tick_failed")


async def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    from app.core.database import async_session_maker
    from app.services import site_config

    async with async_session_maker() as db:
        try:
            interval = await site_config.get_int_or(db, "pve_tick_interval_sec", 20)
        except Exception:
            interval = 20
    interval = max(5, min(300, interval))
    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(_tick_safe, "interval", seconds=interval, id=_JOB_ID, max_instances=1)
    _scheduler.start()
    logger.info("pve_scheduler_started interval_sec=%s", interval)


async def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
    await ENGINE.trader.close()
