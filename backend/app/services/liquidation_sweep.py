"""强制平仓定时扫描。仿 loan_sweep 模式，每 N 秒扫一次 debt>0 用户。

- run_liquidation_sweep_once()：扫一次，也给 admin run-now 复用
- start_scheduler() / stop_scheduler()：FastAPI lifespan 调
- reschedule(interval_sec)：管理员改 site_config 后调用

Deadlock handling: liquidate_user 的 user → outcomes 锁顺序跟 market.py 的
market → outcomes → user 不一致，理论上同 user 自己同时 SELL 时存在环形等待。
sweep 层 catch DBAPI DeadlockError + log + 该 user 跳过本轮，下次再试。
"""
from __future__ import annotations
import asyncio
import logging
import time
from decimal import Decimal, InvalidOperation
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


def _empty_sweep_result(duration_ms: int = 0) -> dict:
    """正常路径的完整字段集 helper —— "无候选用户"等早返回必须用这个，避免字段
    缺失让 ELK/Prometheus exporter 报错或漏指标 (review I-1)。

    跟 sweep 主流程末尾的 result dict 字段集 1:1 对应。
    """
    return {
        "triggered_count": 0,
        "soft_warning_count": 0,
        "errors": 0,
        "deadlocks": 0,
        "recovered_count": 0,
        "skipped_count": 0,
        "sweep_duration_ms": duration_ms,
    }


def _is_deadlock_error(exc: Exception) -> bool:
    """识别 Postgres / SQLite deadlock 类异常。"""
    if isinstance(exc, DBAPIError):
        # Postgres: SQLSTATE 40P01 (deadlock_detected)
        # SQLite: "database is locked" or OperationalError
        msg = str(exc).lower()
        return ("deadlock" in msg) or ("40p01" in msg)
    return False


async def _liquidate_one_user(
    *,
    uid: int,
    hard_thr: Decimal,
    rate: Decimal,
    trigger_source: str,
    now: float,
    sem: asyncio.Semaphore,
    partial_pct: Decimal,
    target_margin: Decimal,
    emergency_threshold: Decimal,
) -> str:
    """阶段 2 内单用户强平 worker，给 asyncio.gather 调用。

    返回值用于 sweep 主函数聚合统计：
    - 'triggered': 成功强平
    - 'recovered': lock 后 margin 已恢复，放过
    - 'skipped': HALT race / debt=0 / etc.
    - 'deadlock': 死锁，跳过该用户本轮
    - 'error': 其他异常

    Semaphore 限并发：防 DB_POOL_SIZE=10 耗尽 + 同 market 锁竞争一窝蜂。
    """
    async with sem:
        try:
            from app.services.market_writer import WRITER
            if WRITER.enabled:
                # margin 判定仍在这里做（复用既有 stage-2 语义），判定通过才编排强平
                async with async_session_maker() as session:
                    async with session.begin():
                        user = await lock_user(session, uid)
                        if user.debt <= Decimal("0"):
                            return "skipped"
                        if await user_has_halt_holdings(session, uid):
                            logger.info("sweep_skip_user_with_halt_holdings",
                                        extra={"user_id": uid, "stage": "stage2_race_guard"})
                            return "skipped"
                        hv_now = (await compute_users_holdings_value(
                            session, user_ids=[uid])).get(uid, Decimal("0"))
                        margin_now = (user.cash - user.debt + hv_now) / user.debt
                        if margin_now >= hard_thr:
                            logger.info("sweep_skip_recovered",
                                        extra={"user_id": uid, "margin_now": float(margin_now)})
                            return "recovered"
                ev = await liquidation_service.liquidate_user_split(
                    uid, daily_rate=rate, trigger_source=trigger_source,
                    partial_pct=partial_pct, target_margin=target_margin,
                    emergency_threshold=emergency_threshold)
                if ev is None:
                    _recently_attempted[uid] = now
                return "triggered"
            # ↓ 老路径原逻辑，一行不动
            async with async_session_maker() as session:
                async with session.begin():
                    user = await lock_user(session, uid)
                    if user.debt <= Decimal("0"):
                        return "skipped"

                    if await user_has_halt_holdings(session, uid):
                        logger.info(
                            "sweep_skip_user_with_halt_holdings",
                            extra={"user_id": uid, "stage": "stage2_race_guard"},
                        )
                        return "skipped"

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
                            extra={"user_id": uid, "margin_now": float(margin_now)},
                        )
                        return "recovered"

                    ev = await liquidation_service.liquidate_user(
                        session, user, daily_rate=rate,
                        trigger_source=trigger_source,
                        partial_pct=partial_pct,
                        target_margin=target_margin,
                        emergency_threshold=emergency_threshold,
                    )
                    if (ev.sold_positions_count == 0
                            and ev.repaid_amount == 0):
                        _recently_attempted[uid] = now
                    return "triggered"
        except DBAPIError as e:
            if _is_deadlock_error(e):
                logger.warning(
                    "liquidation_sweep_deadlock_skipped_this_round",
                    extra={"user_id": uid, "error": str(e)[:200]},
                )
                # 用 cooldown 防 log spam：1s tick 下，pathological 对手方
                # 持续跟该 user 锁竞争会每秒打一条 warning。bump cooldown 让
                # 30min 内不再尝试，给运维时间介入。
                _recently_attempted[uid] = now
                return "deadlock"
            logger.exception(
                "liquidation_sweep_user_dbapi_error", extra={"user_id": uid}
            )
            return "error"
        except Exception:
            logger.exception(
                "liquidation_sweep_user_error", extra={"user_id": uid}
            )
            return "error"


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
                "liquidation_partial_pct",          # 新
                "liquidation_target_margin",        # 新
                "liquidation_emergency_threshold",  # 新
            ])
        except Exception:
            logger.exception("liquidation_sweep_config_load_failed")
            return {"skipped": "config_load_failed"}

        if cfg.get("liquidation_enabled", "false").lower() not in ("true", "1", "yes"):
            return {"skipped": "disabled"}
        if "loan_daily_rate" not in cfg:
            logger.error("liquidation_sweep_no_daily_rate")
            return {"skipped": "no_daily_rate"}
        # 精确捕获 parse 失败的异常类型，避免吞掉编程 bug (review I1)
        try:
            hard_thr = Decimal(cfg["liquidation_hard_threshold"])
            soft_thr = Decimal(cfg["liquidation_soft_threshold"])
            rate = Decimal(cfg["loan_daily_rate"])
            partial_pct = Decimal(cfg["liquidation_partial_pct"])
            target_margin = Decimal(cfg["liquidation_target_margin"])
            emergency_threshold = Decimal(cfg["liquidation_emergency_threshold"])
        except (KeyError, InvalidOperation, ValueError):
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
            return _empty_sweep_result(duration_ms)

        # 过滤掉 cooldown 内的用户，剩余的批量算 holdings_value。
        # 默认用 -inf 而非 0.0：从未被强平的用户必须无条件通过——否则在刚开机
        # （time.monotonic() < _STUCK_COOLDOWN_SEC）的主机上 `0.0 + 1800 <= now` 会判
        # False，把所有用户误过滤掉，sweep 形同失效（开机 30 分钟内强平不工作）。
        active_rows = [
            r for r in candidate_rows
            if _recently_attempted.get(r.id, float("-inf")) + _STUCK_COOLDOWN_SEC <= now
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

    # ===== 阶段 2：仅越线用户并发 lock + 重算 margin + 强平 =====
    # 每个用户独立 session，互不干扰；用 asyncio.gather + Semaphore(3) 限并发
    # 防止连接池 (DB_POOL_SIZE=10) 耗尽 + 同 market 锁竞争被一窝蜂阻塞。
    # 同 user 内部 lock 顺序仍是 user → outcomes (锁顺序跟 BUY/SELL 不同 →
    # 已有 deadlock catch 跳过当轮)。
    triggered = 0
    warned = len(over_soft)
    errors = 0
    deadlocks = 0
    recovered = 0   # lock 后 margin 已恢复，放过（监控 stage 1 stale read 率）
    skipped = 0     # HALT race / debt=0 / 其他守卫跳过

    if over_hard:
        sem = asyncio.Semaphore(3)
        results = await asyncio.gather(
            *[
                _liquidate_one_user(
                    uid=uid, hard_thr=hard_thr, rate=rate,
                    trigger_source=trigger_source, now=now, sem=sem,
                    partial_pct=partial_pct,
                    target_margin=target_margin,
                    emergency_threshold=emergency_threshold,
                )
                for uid in over_hard
            ],
            return_exceptions=False,  # 每个内部已 try/except，外层不会抛
        )
        for r in results:
            if r == "triggered":
                triggered += 1
            elif r == "deadlock":
                deadlocks += 1
            elif r == "error":
                errors += 1
            elif r == "recovered":
                recovered += 1
            elif r == "skipped":
                skipped += 1

    duration_ms = int((time.monotonic() - start_ts) * 1000)
    result = {
        "triggered_count": triggered,
        "soft_warning_count": warned,
        "errors": errors,
        "deadlocks": deadlocks,
        "recovered_count": recovered,  # stage 1 stale read 监控信号
        "skipped_count": skipped,       # HALT race / 其他守卫跳过监控
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
    interval = max(5, min(7200, interval))
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
    interval = max(5, min(7200, interval_sec))
    _scheduler.reschedule_job(
        _JOB_ID, trigger="interval", seconds=interval,
    )
    logger.info("liquidation_sweep_rescheduled", extra={"interval_sec": interval})
