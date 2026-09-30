"""Bounded scheduled scan: stable pages, full coverage, no event triggers."""
import asyncio
import logging
import time
from decimal import Decimal
from sqlalchemy import select, or_, exists
from app.core.database import async_session_maker
from app.models.base import User
from app.models.credit import LiquidationRun
from app.models.fx import FxShortPosition
from app.services import site_config
from app.services.credit.flags import get_flags
from app.services.credit.ownership import OWNERSHIP
from app.services.credit.valuation import RISK_STATUS_OK, value_users_batch
from app.services.credit.execution import execute_user

logger = logging.getLogger(__name__)
PAGE_SIZE = 20
WORKERS = 3
ZERO = Decimal('0')


def _has_foreign_obligation(value):
    """估值里存在空头组 ⇔ 该账户有正的外币本金/利息（spec §8.2 候选）。"""
    return any(group.role == 'short_cover' for group in value.groups)


def _positive_assets(value):
    """权威正资产整组净回收 A（不可执行/阻塞资产按 0 计，见 valuation.py）。"""
    return sum(
        (group.value for group in value.groups
         if group.role == 'asset_sale' and group.value is not None),
        ZERO,
    )


def _triggered(value, thresholds):
    """已知完整估值才比较：共享 E/B 门槛，K 来自权威估值而非金债 D。"""
    return thresholds.triggered_basis(
        equity=value.liquidation_equity,
        debt=value.debt_effective,
        positive_assets=_positive_assets(value),
        short_cover=value.short_cover_cost,
    )


async def run_sweep(trigger_source='scheduler'):
    OWNERSHIP.require_writes()
    start = time.monotonic()
    async with async_session_maker() as session:
        cfg = await site_config.get_many(session, ['liquidation_enabled', 'loan_daily_rate',
            'liquidation_partial_pct', 'liquidation_sweep_interval_sec'])
    if cfg.get('liquidation_enabled', 'false').lower() not in ('true', '1', 'yes'):
        return {'skipped': 'disabled'}
    rate = Decimal(cfg['loan_daily_rate'])
    pct = Decimal(cfg.get('liquidation_partial_pct', '0.1'))
    if not rate.is_finite() or rate < 0 or not pct.is_finite() or not 0 < pct <= 1:
        raise ValueError('invalid liquidation configuration')
    result = dict(triggered_count=0, soft_warning_count=0, errors=0, deadlocks=0,
                  recovered_count=0, skipped_count=0, scanned_count=0,
                  blocked_count=0, monetary_action_count=0,
                  execution_duration_ms=0, max_user_execution_ms=0)
    sem = asyncio.Semaphore(WORKERS)

    async def worker(uid):
        async with sem:
            user_start = time.monotonic()
            status = 'error'
            try:
                status = await execute_user(uid, rate=rate, pct=pct, source=trigger_source)
                key = {'triggered': 'triggered_count', 'recovered': 'recovered_count'}.get(status, 'skipped_count')
                result[key] += 1
                if status == 'blocked':
                    result['blocked_count'] += 1
                elif status == 'triggered':
                    result['monetary_action_count'] += 1
            except Exception as exc:
                from app.services.liquidation_sweep import _is_deadlock_error
                result['errors'] += 1
                if _is_deadlock_error(exc):
                    result['deadlocks'] += 1
                    status = 'deadlock'
                logger.exception('unified liquidation user failed: %s', uid)
            finally:
                # End-to-end execution includes gate/queue wait; this is NOT a
                # row-lock hold metric, which requires lower-layer tracing.
                elapsed_ms = int((time.monotonic() - user_start) * 1000)
                result['execution_duration_ms'] += elapsed_ms
                result['max_user_execution_ms'] = max(result['max_user_execution_ms'], elapsed_ms)
                logger.info('unified liquidation user complete', extra={
                    'user_id': uid, 'execution_status': status, 'execution_duration_ms': elapsed_ms})
    cursor = 0
    # Capture upper bound so concurrent new users cannot extend a scan forever.
    async with async_session_maker() as session:
        from sqlalchemy import func
        upper = (await session.execute(select(func.max(User.id)))).scalar() or 0
    while cursor < upper:
        async with async_session_maker() as session:
            active = exists(select(LiquidationRun.id).where(
                LiquidationRun.user_id == User.id, LiquidationRun.status == 'active'))
            # 外币本金/利息任一 > 0 也有义务，即使 User.debt=0 且没有活跃 run。
            foreign_debt = exists(select(FxShortPosition.id).where(
                FxShortPosition.user_id == User.id,
                or_(FxShortPosition.principal_foreign > ZERO,
                    FxShortPosition.interest_foreign > ZERO)))
            ids = list((await session.execute(select(User.id).where(User.id > cursor,
                User.id <= upper, or_(User.debt > 0, active, foreign_debt)).order_by(User.id).limit(PAGE_SIZE))).scalars())
            if not ids:
                break
            values = await value_users_batch(session, ids, daily_rate=rate)
            active_ids = set((await session.execute(select(LiquidationRun.user_id).where(
                LiquidationRun.user_id.in_(ids), LiquidationRun.status == 'active'))).scalars())
            thresholds = get_flags().thresholds
            candidates = []
            for uid in ids:
                value = values.get(uid)
                if value is None:
                    continue
                if uid in active_ids:
                    candidates.append(uid)
                    continue
                # 未知 K：不能比门槛、不能当 0；有外币义务就送执行器建/续 run。
                if value.risk_status != RISK_STATUS_OK:
                    if _has_foreign_obligation(value):
                        candidates.append(uid)
                    continue
                if _triggered(value, thresholds):
                    candidates.append(uid)
        # Session/connection released before workers wait for gates or writer queues.
        result['scanned_count'] += len(ids)
        cursor = ids[-1]
        await asyncio.gather(*(worker(uid) for uid in candidates))
        await asyncio.sleep(0)
    result['sweep_duration_ms'] = int((time.monotonic()-start)*1000)
    if result['sweep_duration_ms'] > int(cfg.get('liquidation_sweep_interval_sec', '600'))*1000:
        logger.warning('liquidation scan exceeded interval: %s', result)
    logger.info('unified liquidation scan complete: %s', result)
    return result
