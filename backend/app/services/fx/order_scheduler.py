"""Periodic private FX order scan; observations are fixed for each batch."""
import asyncio
import logging
from decimal import Decimal, ROUND_HALF_EVEN

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.fx import FxPair, FxTriggerOrder
from app.services import site_config
from app.services.account_events import enqueue_order_result
from app.services.credit import flags
from app.services.credit.ownership import OWNERSHIP
from app.services.fx import orders
from app.services.fx.amm import marginal_price

logger = logging.getLogger(__name__)
_scheduler = None
_JOB_ID = 'fx_order_scan'
_active_scan = None


async def run_order_scan_once() -> dict[str, int]:
    OWNERSHIP.require_writes()
    stats = dict(checked=0, triggered=0, succeeded=0, failed=0)
    async with async_session_maker() as db:
        rows = (await db.execute(select(
            FxTriggerOrder.id, FxTriggerOrder.comparison, FxTriggerOrder.trigger_price,
            FxPair.status, FxPair.archived, FxPair.gold_reserve, FxPair.foreign_reserve,
        ).join(FxPair, FxPair.id == FxTriggerOrder.pair_id)
            .where(FxTriggerOrder.status == 'waiting').order_by(FxTriggerOrder.id))).all()
        observations = []
        for oid, comparison, trigger, status, archived, gold, foreign in rows:
            stats['checked'] += 1
            if archived or status == 'closed':
                observations.append((oid, None, True))
                continue
            try:
                price = marginal_price(gold, foreign).quantize(Decimal('0.000001'), rounding=ROUND_HALF_EVEN)
            except (ValueError, ArithmeticError):
                continue
            if price.is_finite() and price > 0 and (price <= trigger if comparison == 'lte' else price >= trigger):
                observations.append((oid, price, False))
    for oid, price, permanent in observations:
        OWNERSHIP.require_writes()
        if permanent:
            outcome = await orders.fail_unavailable_order(oid)
        else:
            stats['triggered'] += 1
            outcome = await orders.attempt_order(oid, price)
        if outcome is None:
            continue
        stats[outcome.status] += 1
        try:
            async with async_session_maker() as db:
                payload = await orders.result_for_order(db, outcome)
            enqueue_order_result(outcome.user_id, payload)
        except Exception:
            logger.exception('Committed private FX order result dropped')
    return stats


async def _tick_safe():
    global _active_scan
    _active_scan = asyncio.create_task(run_order_scan_once())
    try:
        await asyncio.shield(_active_scan)
    except Exception:
        logger.exception('FX order scan failed')
    finally:
        if _active_scan is not None and _active_scan.done():
            _active_scan = None


async def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None or not OWNERSHIP.writes_enabled or not flags.write_schedulers_enabled():
        return
    async with async_session_maker() as db:
        interval = await site_config.get_int_or(db, 'fx_order_scan_interval_sec', 10)
    _scheduler = AsyncIOScheduler(timezone='UTC')
    _scheduler.add_job(_tick_safe, 'interval', seconds=max(5, min(3600, interval)),
                       id=_JOB_ID, max_instances=1, coalesce=True)
    _scheduler.start()


async def stop_scheduler() -> None:
    global _scheduler, _active_scan
    if _scheduler is not None:
        _scheduler.pause()
        _scheduler.remove_all_jobs()
    try:
        if _active_scan is not None:
            await asyncio.shield(_active_scan)
    finally:
        _active_scan = None
        if _scheduler is not None:
            _scheduler.shutdown(wait=False)
            _scheduler = None


async def reschedule(interval_sec: int) -> None:
    if _scheduler is not None:
        _scheduler.reschedule_job(_JOB_ID, trigger='interval', seconds=max(5, min(3600, interval_sec)))
