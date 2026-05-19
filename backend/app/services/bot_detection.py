"""Anti-bot L4: 行为监控信号算法 + scheduler。

详见 docs/superpowers/specs/2026-05-20-anti-bot-design.md。

本文件分两部分：
1. 信号算法（pure functions，Task 9 + Task 10）
2. scheduler + 主循环（Task 11 + Task 12 实施）

每 30 min 扫近 2h Transaction，触发任一信号 → 写 bot_suspicion。
白名单 user 跳过扫描；6h 内同信号同 user 不重写。
"""
from __future__ import annotations
import json
import logging
import statistics
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# UTC+8 凌晨 03-06 = UTC 19-22 (前一天)
LATE_NIGHT_UTC_HOURS = {19, 20, 21}  # 19:00-22:00 UTC = 03:00-06:00 CST

_logger = logging.getLogger("thccb.bot_detection")
_DEDUP_WINDOW_SEC = 6 * 3600  # 6 小时 dedup


def compute_high_freq_signal(transactions: Iterable, threshold: int) -> bool:
    """高频信号：窗口内 user 交易总笔数 ≥ threshold。"""
    return sum(1 for _ in transactions) >= threshold


def compute_late_night_signal(transactions: Iterable, threshold: int) -> bool:
    """凌晨信号：03-06 CST (= 19-22 UTC) 时段交易笔数 ≥ threshold。"""
    count = sum(
        1 for tx in transactions
        if tx.timestamp.astimezone(timezone.utc).hour in LATE_NIGHT_UTC_HOURS
    )
    return count >= threshold


def compute_regular_interval_signal(
    transactions: list, stddev_ms_threshold: int,
) -> bool:
    """间隔规律性信号：≥ 3 笔交易 + 相邻间隔的 stddev_ms < threshold。

    少于 3 笔不算（样本太小）。
    """
    if len(transactions) < 3:
        return False
    sorted_txns = sorted(transactions, key=lambda t: t.timestamp)
    intervals_ms = []
    for i in range(1, len(sorted_txns)):
        delta = sorted_txns[i].timestamp - sorted_txns[i - 1].timestamp
        intervals_ms.append(delta.total_seconds() * 1000)
    if len(intervals_ms) < 2:
        return False
    sd = statistics.stdev(intervals_ms)
    return sd < stddev_ms_threshold


def compute_fast_follow_signal(
    all_transactions: list,
    *,
    target_user_id: int,
    trigger_cost_threshold: Decimal,
    latency_ms: int,
    count_threshold: int,
) -> bool:
    """fast_follow 信号: target user 在别人大单后 latency 内同 market 跟进 ≥ count 次。

    - trigger: any user (除 target user 自己) 的交易 abs(cost) ≥ trigger_cost_threshold
    - 跟进: target user 在 trigger 之后 latency_ms 内、同 market_id 的交易
    - 阈值: 跟进次数 ≥ count_threshold

    LMSR 卖出 cost 为负，所以用 abs(cost)。

    详见 spec § L4 信号算法。
    """
    # 提取 triggers（排除自己的）
    triggers = [
        tx for tx in all_transactions
        if abs(float(tx.cost)) >= float(trigger_cost_threshold)
        and tx.user_id != target_user_id
    ]
    if not triggers:
        return False

    # target user 在各 market 上的交易
    target_txns = [tx for tx in all_transactions if tx.user_id == target_user_id]

    follow_count = 0
    for trigger in triggers:
        deadline = trigger.timestamp + timedelta(milliseconds=latency_ms)
        # 找 target user 在 trigger 之后 latency 内同 market 的最近一笔
        for tt in target_txns:
            if (
                tt.market_id == trigger.market_id
                and trigger.timestamp < tt.timestamp <= deadline
            ):
                follow_count += 1
                break  # 一个 trigger 只算一次跟进

    return follow_count >= count_threshold


# ─────────────────────────────────────────────────────────────────────────────
# 主循环 (Task 11)
# ─────────────────────────────────────────────────────────────────────────────


def _empty_result(enabled: bool, duration_ms: int = 0) -> dict:
    """完整字段集 helper —— 早返回路径必须用这个，跟主流程末尾 dict 字段集 1:1 对齐。

    详见 docs/schema-conventions.md "早返回 dict 字段一致性"。
    """
    return {
        "enabled": enabled,
        "scanned_users": 0,
        "triggered_count": 0,
        "skipped_count": 0,
        "duration_ms": duration_ms,
    }


async def _recent_signal_exists(
    db: "AsyncSession", user_id: int, signal: str, now: datetime,
) -> bool:
    """6h 内是否已有 pending BotSuspicion 含此 signal。"""
    # 延迟导入避免 module-import-time 循环
    from app.models.base import BotSuspicion

    threshold = now - timedelta(seconds=_DEDUP_WINDOW_SEC)
    stmt = (
        select(BotSuspicion.id)
        .where(
            BotSuspicion.user_id == user_id,
            BotSuspicion.review_status == "pending",
            BotSuspicion.triggered_at > threshold,
            BotSuspicion.signals.contains(signal),
        )
        .limit(1)
    )
    return (await db.execute(stmt)).scalar() is not None


async def run_bot_detection_once() -> dict:
    """每 30 min 一次：扫近 window_sec 的 Transaction，触发任一信号 → 写 BotSuspicion。

    白名单 user 跳过；同 user 同 signal 6h 内已 pending 不重写。

    返回 dict 含完整字段：enabled, scanned_users, triggered_count, skipped_count,
    duration_ms。早返回路径用 _empty_result helper 保证字段一致性
    (review I-1 教训 / docs/schema-conventions.md)。
    """
    # 延迟导入：bot_detection.py 在 app 启动早期被 schedule 模块引用，
    # 避免与 app.models / app.services.site_config 形成 import cycle。
    from app.core.database import async_session_maker
    from app.models.base import BotSuspicion, Outcome, Transaction
    from app.services import site_config
    from app.services.anti_bot import parse_whitelist

    start_ts = time.monotonic()
    now = datetime.now(timezone.utc)

    async with async_session_maker() as db:
        try:
            cfg = await site_config.get_many(db, [
                "bot_detection_enabled", "bot_detection_window_sec",
                "bot_freq_threshold", "bot_late_night_threshold",
                "bot_interval_stddev_ms_threshold",
                "bot_fast_follow_trigger_cost",
                "bot_fast_follow_latency_ms",
                "bot_fast_follow_count_threshold",
                "quant_whitelist_user_ids",
            ])
        except Exception:
            _logger.exception("bot_detection_config_load_failed")
            return _empty_result(False, int((time.monotonic() - start_ts) * 1000))

        enabled = cfg.get("bot_detection_enabled", "false").lower() in ("true", "1", "yes")
        if not enabled:
            return _empty_result(False, int((time.monotonic() - start_ts) * 1000))

        try:
            window_sec = int(cfg["bot_detection_window_sec"])
            freq_thr = int(cfg["bot_freq_threshold"])
            late_thr = int(cfg["bot_late_night_threshold"])
            stddev_thr = int(cfg["bot_interval_stddev_ms_threshold"])
            ff_cost = Decimal(cfg["bot_fast_follow_trigger_cost"])
            ff_lat = int(cfg["bot_fast_follow_latency_ms"])
            ff_count = int(cfg["bot_fast_follow_count_threshold"])
        except (KeyError, ValueError, Exception):
            _logger.exception("bot_detection_config_parse_failed")
            return _empty_result(True, int((time.monotonic() - start_ts) * 1000))

        whitelist = parse_whitelist(cfg.get("quant_whitelist_user_ids", ""))

        window_start = now - timedelta(seconds=window_sec)
        all_txns_stmt = (
            select(Transaction)
            .where(Transaction.timestamp >= window_start)
            .order_by(Transaction.timestamp.asc())
        )
        all_txns = (await db.execute(all_txns_stmt)).scalars().all()

        # outcome → market 映射（fast_follow 信号要按 market 分组）。
        # SQLModel/Pydantic 不允许在 ORM 实例上 setattr 非声明字段，所以用
        # SimpleNamespace 浅 wrap 一层，给 compute_fast_follow_signal 暴露 market_id。
        from types import SimpleNamespace

        outcome_ids = {tx.outcome_id for tx in all_txns}
        o_to_m: dict[int, int] = {}
        if outcome_ids:
            outcomes_rows = (await db.execute(
                select(Outcome.id, Outcome.market_id)
                .where(Outcome.id.in_(outcome_ids))
            )).all()
            o_to_m = {r[0]: r[1] for r in outcomes_rows}

        all_txns_wrapped = [
            SimpleNamespace(
                user_id=tx.user_id,
                outcome_id=tx.outcome_id,
                market_id=o_to_m.get(tx.outcome_id),
                cost=tx.cost,
                timestamp=tx.timestamp,
            )
            for tx in all_txns
        ]

        # 按 user_id 分组（拿 wrapped 的，下游函数只需 timestamp / market_id / cost）
        txns_by_user: dict[int, list] = {}
        for tx in all_txns_wrapped:
            txns_by_user.setdefault(tx.user_id, []).append(tx)

        scanned_users = 0
        triggered_count = 0
        skipped_count = 0

        for uid, user_txns in txns_by_user.items():
            if uid in whitelist:
                skipped_count += 1
                continue
            scanned_users += 1

            triggered_signals: list[str] = []
            metrics: dict = {"total_trades": len(user_txns)}

            if compute_high_freq_signal(user_txns, freq_thr):
                triggered_signals.append("high_freq")
            if compute_late_night_signal(user_txns, late_thr):
                triggered_signals.append("late_night")
                metrics["late_night_trades"] = sum(
                    1 for tx in user_txns
                    if tx.timestamp.astimezone(timezone.utc).hour in LATE_NIGHT_UTC_HOURS
                )
            if compute_regular_interval_signal(user_txns, stddev_thr):
                triggered_signals.append("regular_interval")
            if compute_fast_follow_signal(
                all_txns_wrapped, target_user_id=uid,
                trigger_cost_threshold=ff_cost,
                latency_ms=ff_lat, count_threshold=ff_count,
            ):
                triggered_signals.append("fast_follow")

            if not triggered_signals:
                continue

            # 6h dedup
            new_signals = []
            for sig in triggered_signals:
                if not await _recent_signal_exists(db, uid, sig, now):
                    new_signals.append(sig)
            if not new_signals:
                continue

            ev = BotSuspicion(
                user_id=uid,
                triggered_at=now,
                signals=",".join(new_signals),
                metrics=json.dumps(metrics),
                window_start=window_start,
                window_end=now,
            )
            db.add(ev)
            await db.flush()
            triggered_count += 1
            _logger.warning(
                "bot_suspicion_recorded",
                extra={"user_id": uid, "signals": ",".join(new_signals)},
            )

        await db.commit()

    duration_ms = int((time.monotonic() - start_ts) * 1000)
    result = {
        "enabled": True,
        "scanned_users": scanned_users,
        "triggered_count": triggered_count,
        "skipped_count": skipped_count,
        "duration_ms": duration_ms,
    }
    _logger.info("bot_detection_done", extra=result)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler lifecycle (Task 12)
# ─────────────────────────────────────────────────────────────────────────────

_scheduler: Optional[AsyncIOScheduler] = None
_JOB_ID = "bot_detection_tick"


async def _tick_safe():
    try:
        await run_bot_detection_once()
    except Exception:
        _logger.exception("bot_detection_tick_failed")


async def start_scheduler() -> None:
    """FastAPI lifespan 调。从 site_config 读 interval 启动 scheduler。"""
    global _scheduler
    if _scheduler is not None:
        return
    from app.core.database import async_session_maker
    from app.services import site_config
    async with async_session_maker() as db:
        try:
            interval = await site_config.get_int(db, "bot_detection_interval_sec")
        except Exception:
            interval = 1800
    interval = max(60, min(7200, interval))
    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(
        _tick_safe, "interval", seconds=interval,
        id=_JOB_ID, max_instances=1,
    )
    _scheduler.start()
    _logger.info("bot_detection_scheduler_started", extra={"interval_sec": interval})


async def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


async def reschedule(interval_sec: int) -> None:
    """管理员改 site_config 后调，热调度。"""
    global _scheduler
    if _scheduler is None:
        return
    interval = max(60, min(7200, interval_sec))
    _scheduler.reschedule_job(_JOB_ID, trigger="interval", seconds=interval)
    _logger.info("bot_detection_scheduler_rescheduled", extra={"interval_sec": interval})
