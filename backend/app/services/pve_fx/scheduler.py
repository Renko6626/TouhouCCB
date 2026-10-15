"""Write-owner FX timer; drain its current tick before downstream shutdown."""
import asyncio
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.services.pve_fx.engine import get_fx_pve_engine

logger = logging.getLogger('thccb.pve_fx')
_scheduler = None
_active_tick = None


async def _tick_safe():
    global _active_tick
    _active_tick = asyncio.current_task()
    try:
        await get_fx_pve_engine().tick()
    except Exception:
        logger.exception('fx_pve_tick_failed')
    finally:
        _active_tick = None


async def start_fx_pve_scheduler() -> None:
    global _scheduler
    from app.services.credit.ownership import OWNERSHIP
    from app.services.credit import flags
    if _scheduler is not None or not OWNERSHIP.is_owner or not flags.write_schedulers_enabled():
        return
    _scheduler = AsyncIOScheduler(timezone='UTC')
    _scheduler.add_job(_tick_safe, 'interval', seconds=5, id='fx_pve_tick',
                       max_instances=1, coalesce=True)
    _scheduler.start()


async def stop_fx_pve_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.pause()
        _scheduler.remove_all_jobs()
    try:
        if _active_tick is not None:
            await asyncio.shield(_active_tick)
    finally:
        if _scheduler is not None:
            _scheduler.shutdown(wait=False)
            _scheduler = None
