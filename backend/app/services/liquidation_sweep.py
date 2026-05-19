"""强制平仓定时扫描。仿 loan_sweep 模式，每 N 秒扫一次 debt>0 用户。

- run_liquidation_sweep_once()：扫一次，也给 admin run-now 复用
- start_scheduler() / stop_scheduler()：FastAPI lifespan 调
- reschedule(interval_sec)：管理员改 site_config 后调用

Deadlock handling: liquidate_user 的 user → outcomes 锁顺序跟 market.py 的
market → outcomes → user 不一致，理论上同 user 自己同时 SELL 时存在环形等待。
sweep 层 catch DBAPI DeadlockError + log + 该 user 跳过本轮，下次再试。
"""
from __future__ import annotations
import logging
import time
from decimal import Decimal
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.core.database import async_session_maker
from app.models.base import Market, MarketStatus, Outcome, Position, User
from app.services import liquidation_service, site_config
from app.services.market_locks import lock_user
from app.services.wealth import compute_users_holdings_value


async def _user_has_non_trading_holdings(session, user_id: int) -> bool:
    """检测 user 是否在 HALT/RESOLVED 市场仍有 amount>0 持仓。

    用于"流动性危机保护"守卫：HALT 期间这些持仓的 LCV 估值被算 0，但用户的总
    资产可能仍健康。盲目按 LCV margin 强平会平掉用户的 TRADING 持仓导致滑点
    损失 + 失去后续机会（LTCM 1998 经典场景）。

    假设：HALT/RESOLVED 状态由 admin 控制，持续时间短，不存在用户故意持有 HALT
    持仓豁免强平的滥用空间。
    """
    stmt = (
        select(Position.id)
        .join(Outcome, Outcome.id == Position.outcome_id)
        .join(Market, Market.id == Outcome.market_id)
        .where(
            Position.user_id == user_id,
            Position.amount > 0,
            Market.status != MarketStatus.TRADING,
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar() is not None


logger = logging.getLogger("thccb.liquidation_sweep")

# 模块级防爆 cache：已扫过但没动到 user 的 30 min 内跳过
_recently_attempted: dict[int, float] = {}
_STUCK_COOLDOWN_SEC = 1800

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


async def run_liquidation_sweep_once(trigger_source: str = "scheduler") -> dict:
    """扫一次全体 debt>0 用户。给 scheduler + admin run-now 共用。

    trigger_source: "scheduler"（定时 cron 触发）或 "admin_manual"（管理员手动触发）。
    """
    start_ts = time.monotonic()
    async with async_session_maker() as session:
        enabled = await site_config.get_bool(session, "liquidation_enabled")
        if not enabled:
            return {"skipped": "disabled"}
        hard_thr = await site_config.get_decimal(
            session, "liquidation_hard_threshold"
        )
        soft_thr = await site_config.get_decimal(
            session, "liquidation_soft_threshold"
        )
        try:
            rate = await site_config.get_decimal(session, "loan_daily_rate")
        except Exception:
            logger.exception("liquidation_sweep_no_daily_rate")
            return {"skipped": "no_daily_rate"}

    async with async_session_maker() as session:
        ids = (
            await session.execute(select(User.id).where(User.debt > 0))
        ).scalars().all()

    triggered = 0
    warned = 0
    errors = 0
    deadlocks = 0
    now = time.monotonic()

    for uid in ids:
        last_attempt = _recently_attempted.get(uid, 0.0)
        if last_attempt + _STUCK_COOLDOWN_SEC > now:
            continue

        try:
            async with async_session_maker() as session:
                async with session.begin():
                    user = await lock_user(session, uid)
                    if user.debt <= Decimal("0"):
                        continue

                    # 流动性危机保护：用户在 HALT/RESOLVED 市场有持仓时跳过本轮，
                    # 等市场恢复 TRADING 后再判定 margin。详见 _user_has_non_trading_holdings
                    # docstring 和 docs/holdings-value-semantics.md。
                    if await _user_has_non_trading_holdings(session, uid):
                        logger.info(
                            "sweep_skip_user_with_halt_holdings",
                            extra={"user_id": uid},
                        )
                        continue

                    hv = (
                        await compute_users_holdings_value(
                            session, user_ids=[uid]
                        )
                    ).get(uid, Decimal("0"))
                    nw = user.cash - user.debt + hv
                    margin = nw / user.debt

                    if margin < hard_thr:
                        ev = await liquidation_service.liquidate_user(
                            session, user, daily_rate=rate,
                            trigger_source=trigger_source,
                        )
                        triggered += 1
                        if (ev.sold_positions_count == 0
                                and ev.repaid_amount == 0):
                            _recently_attempted[uid] = now
                    elif margin < soft_thr:
                        warned += 1
                        logger.warning(
                            "margin_call_soft_threshold",
                            extra={
                                "user_id": uid,
                                "margin_ratio": float(margin),
                                "soft_threshold": float(soft_thr),
                            },
                        )
        except DBAPIError as e:
            if _is_deadlock_error(e):
                deadlocks += 1
                logger.warning(
                    "liquidation_sweep_deadlock_skipped_this_round",
                    extra={"user_id": uid, "error": str(e)[:200]},
                )
            else:
                errors += 1
                logger.exception(
                    "liquidation_sweep_user_dbapi_error",
                    extra={"user_id": uid},
                )
        except Exception:
            errors += 1
            logger.exception(
                "liquidation_sweep_user_error",
                extra={"user_id": uid},
            )

    duration_ms = int((time.monotonic() - start_ts) * 1000)
    result = {
        "triggered_count": triggered,
        "soft_warning_count": warned,
        "errors": errors,
        "deadlocks": deadlocks,
        "sweep_duration_ms": duration_ms,
    }
    logger.info("liquidation_sweep_done", extra=result)
    return result


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
    interval = max(60, min(7200, interval))
    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(
        _tick_safe, "interval", seconds=interval,
        id=_JOB_ID, max_instances=1,
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
    interval = max(60, min(7200, interval_sec))
    _scheduler.reschedule_job(
        _JOB_ID, trigger="interval", seconds=interval,
    )
    logger.info("liquidation_sweep_rescheduled", extra={"interval_sec": interval})
