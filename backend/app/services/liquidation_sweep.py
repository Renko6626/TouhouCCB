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
from app.services.wealth import compute_users_holdings_value, user_has_halt_holdings


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
        # 一次批量取 4 个 key，省 3 个 round trip vs 4 次串行 get_*
        try:
            cfg = await site_config.get_many(session, [
                "liquidation_enabled",
                "liquidation_hard_threshold",
                "liquidation_soft_threshold",
                "loan_daily_rate",
            ])
        except Exception:
            logger.exception("liquidation_sweep_config_load_failed")
            return {"skipped": "config_load_failed"}

        if cfg.get("liquidation_enabled", "false").lower() not in ("true", "1", "yes"):
            return {"skipped": "disabled"}
        if "loan_daily_rate" not in cfg:
            logger.error("liquidation_sweep_no_daily_rate")
            return {"skipped": "no_daily_rate"}
        try:
            hard_thr = Decimal(cfg["liquidation_hard_threshold"])
            soft_thr = Decimal(cfg["liquidation_soft_threshold"])
            rate = Decimal(cfg["loan_daily_rate"])
        except (KeyError, Exception):
            logger.exception("liquidation_sweep_config_parse_failed")
            return {"skipped": "config_parse_failed"}

    # ===== 阶段 1：read-only 批量算 margin，零锁 =====
    # 用一个 session 跑完所有候选用户的预筛 + holdings_value 批量计算，
    # 期间不持有任何 FOR UPDATE 锁，对 BUY/SELL 零阻塞。
    now = time.monotonic()
    over_hard: list[int] = []
    over_soft: list[tuple[int, Decimal]] = []
    async with async_session_maker() as session:
        # 候选用户预筛：debt>0 且没有 HALT 持仓（流动性危机保护）
        from sqlalchemy import exists  # 局部 import 避免污染模块顶层
        halt_pos_subq = (
            select(Position.id)
            .join(Outcome, Outcome.id == Position.outcome_id)
            .join(Market, Market.id == Outcome.market_id)
            .where(
                Position.user_id == User.id,
                Position.amount > 0,
                Market.status == MarketStatus.HALT,
            )
        )
        candidate_rows = (
            await session.execute(
                select(User.id, User.cash, User.debt)
                .where(User.debt > 0)
                .where(~exists(halt_pos_subq))
            )
        ).all()

        if not candidate_rows:
            duration_ms = int((time.monotonic() - start_ts) * 1000)
            return {
                "triggered_count": 0, "soft_warning_count": 0,
                "errors": 0, "deadlocks": 0,
                "sweep_duration_ms": duration_ms,
            }

        # 过滤掉 cooldown 内的用户，剩余的批量算 holdings_value
        active_rows = [
            r for r in candidate_rows
            if _recently_attempted.get(r.id, 0.0) + _STUCK_COOLDOWN_SEC <= now
        ]
        active_uids = [r.id for r in active_rows]

        # 关键：一次批量算所有候选用户的 LCV holdings（之前是 N 个独立 query × N 用户）
        hvs = await compute_users_holdings_value(session, user_ids=active_uids)

        # Python 端筛 over_hard / over_soft
        for r in active_rows:
            hv = hvs.get(r.id, Decimal("0"))
            nw = r.cash - r.debt + hv
            margin = nw / r.debt
            if margin < hard_thr:
                over_hard.append(r.id)
            elif margin < soft_thr:
                over_soft.append((r.id, margin))

    # 软阈值仅 log warning，不需要加锁
    for uid, margin in over_soft:
        logger.warning(
            "margin_call_soft_threshold",
            extra={
                "user_id": uid,
                "margin_ratio": float(margin),
                "soft_threshold": float(soft_thr),
            },
        )

    triggered = 0
    warned = len(over_soft)
    errors = 0
    deadlocks = 0

    # ===== 阶段 2：仅越线用户 lock + 重算 margin + 强平 =====
    # 重算 margin 防 stale read：阶段 1 read 到的 user state 可能在 read → lock 之间
    # 被并发交易/还债改了，如果实际已恢复 healthy 就放过。
    for uid in over_hard:
        try:
            async with async_session_maker() as session:
                async with session.begin():
                    user = await lock_user(session, uid)
                    if user.debt <= Decimal("0"):
                        continue

                    # 阶段 2 HALT 守卫（defense-in-depth）：阶段 1 已预筛，
                    # 这里防 race —— market 在 read → lock 之间被 admin halt。
                    if await user_has_halt_holdings(session, uid):
                        logger.info(
                            "sweep_skip_user_with_halt_holdings",
                            extra={"user_id": uid, "stage": "stage2_race_guard"},
                        )
                        continue

                    # 重算 margin —— lock 后值才稳定
                    hv_now = (
                        await compute_users_holdings_value(
                            session, user_ids=[uid]
                        )
                    ).get(uid, Decimal("0"))
                    margin_now = (user.cash - user.debt + hv_now) / user.debt
                    if margin_now >= hard_thr:
                        logger.info(
                            "sweep_skip_recovered",
                            extra={
                                "user_id": uid,
                                "margin_now": float(margin_now),
                            },
                        )
                        continue  # 已恢复，放过

                    ev = await liquidation_service.liquidate_user(
                        session, user, daily_rate=rate,
                        trigger_source=trigger_source,
                    )
                    triggered += 1
                    if (ev.sold_positions_count == 0
                            and ev.repaid_amount == 0):
                        _recently_attempted[uid] = now
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
