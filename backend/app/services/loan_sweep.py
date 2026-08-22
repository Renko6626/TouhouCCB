"""APS 定时结息 sweep。
- run_sweep_once()：扫一次全体欠债用户
- start_scheduler() / stop_scheduler()：FastAPI lifespan 里调
- reschedule(interval_sec)：管理员改间隔后调用
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.database import async_session_maker
from app.models.base import User
from app.services.loan_service import accrue_interest, _compat_now
from app.services import site_config
from app.services import audit_service


logger = logging.getLogger("thccb.loan_sweep")

_scheduler: Optional[AsyncIOScheduler] = None
_JOB_ID = "loan_sweep_tick"


async def run_sweep_once() -> int:
    """扫一次全体 debt>0 用户，accrue 并 commit。返回结息户数。"""
    async with async_session_maker() as session:
        try:
            rate = await site_config.get_decimal(session, "loan_daily_rate")
        except Exception:
            logger.exception("sweep skip: rate lookup failed")
            return 0
    if rate <= 0:
        logger.debug("sweep skip: rate<=0")
        return 0

    # 定时结息的折叠窗口：距上次结息不足该秒数的用户本 tick 跳过。
    # 利息是按 (1+r)^(Δt/天) 复利的闭式公式，借/还/强平路径都会在自己的时刻精确
    # 结到秒，定时 sweep 只是让展示的 debt 不陈旧——没必要每 60s 给每个债务人
    # 写一条 interest_accrual 审计事件（200 债务人 ≈ 29 万行/天，审计 M1）。
    async with async_session_maker() as session:
        min_gap_sec = await site_config.get_int_or(session, "loan_sweep_min_accrual_sec", 3600)

    touched = 0
    async with async_session_maker() as session:
        result = await session.execute(select(User.id).where(User.debt > 0))
        ids = list(result.scalars().all())

    for uid in ids:
        async with async_session_maker() as session:
            async with session.begin():
                result = await session.execute(
                    select(User).where(User.id == uid).with_for_update()
                )
                u = result.scalar_one()
                before = u.debt
                before_at = u.debt_last_accrued_at
                now = _compat_now(u)
                if before_at is not None and (now - before_at).total_seconds() < min_gap_sec:
                    continue
                accrue_interest(u, rate, now)
                if u.debt != before:
                    session.add(u)
                    touched += 1
                    audit_service.record(
                        session, "interest_accrual",
                        user_id=u.id,
                        payload={
                            "debt_before": before,
                            "debt_after": u.debt,
                            "interest": (u.debt - before),
                            "daily_rate": rate,
                            "elapsed_sec": (now - before_at).total_seconds() if before_at else None,
                            "source": "scheduler",
                        },
                        user_after=audit_service.user_snapshot(u),
                    )
    if touched:
        logger.info("sweep tick: touched=%s", touched)
    return touched


async def _tick_safe():
    try:
        await run_sweep_once()
    except Exception:
        logger.exception("loan sweep tick failed")


async def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    async with async_session_maker() as session:
        try:
            interval = await site_config.get_int(session, "loan_sweep_interval_sec")
        except Exception:
            interval = 60
    interval = max(10, min(3600, interval))
    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(_tick_safe, "interval", seconds=interval, id=_JOB_ID, max_instances=1)
    _scheduler.start()
    logger.info("loan sweep started interval=%ss", interval)


async def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


async def reschedule(interval_sec: int) -> None:
    global _scheduler
    if _scheduler is None:
        return
    interval = max(10, min(3600, interval_sec))
    _scheduler.reschedule_job(_JOB_ID, trigger="interval", seconds=interval)
    logger.info("loan sweep rescheduled interval=%ss", interval)
