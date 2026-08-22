"""赛季重置：保留用户/配置/称号/兑换码库存，清掉全部活动数据与账单，现金还原。

    python scripts/season_reset.py --dry-run     # 只打印将清理的行数与将重置的用户数
    python scripts/season_reset.py               # 交互输入 RESET 才执行；单事务

保留：user、siteconfig、title / title_code_batch / title_code / user_title、
      redemption_partner / redemption_batch / redemption_code（含已售出记录，作库存历史）、alembic_version
清空：market、outcome、position、transaction、outcome_candle、market_required_title、
      ledger_entry、liquidation_events、bot_suspicion、redemption_transaction、danmuku_exchange、audit_event
用户：cash = site_config.initial_balance，debt = 0，debt_last_accrued_at / last_liquidated_at = NULL；
      每人写一条 user_register(source=season_reset) 审计事件，让新赛季事件流从 seq=1 起全员锚定。

**先备份**：docker compose exec -T postgres pg_dump -U thccb thccb > backups/thccb_pre_season_<ts>.sql
**先停后端**：docker compose stop backend（writer 内存状态 / 结息 sweep 不能与本脚本并发）
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import delete, func, select, text, update  # noqa: E402

from app.core.database import async_session_maker, engine  # noqa: E402
import app.models.audit  # noqa: F401, E402
import app.models.ledger  # noqa: F401, E402
import app.models.redemption  # noqa: F401, E402
from app.models.audit import AuditEvent  # noqa: E402
from app.models.base import (  # noqa: E402
    BotSuspicion, LiquidationEvent, Market, Outcome, OutcomeCandle, Position, Transaction, User,
)
from app.models.ledger import LedgerEntry  # noqa: E402
from app.models.redemption import DanmukuExchange, RedemptionTransaction  # noqa: E402
from app.models.title import MarketRequiredTitle  # noqa: E402
from app.services import audit_replay, audit_service, site_config  # noqa: E402

# 子表在前，父表在后（外键顺序）
CLEAR_ORDER = [
    AuditEvent, BotSuspicion, DanmukuExchange, RedemptionTransaction,
    LiquidationEvent, LedgerEntry, Transaction, Position, OutcomeCandle,
    MarketRequiredTitle, Outcome, Market,
]


async def _counts(session) -> dict[str, int]:
    out = {}
    for model in CLEAR_ORDER:
        out[model.__tablename__] = int((await session.execute(
            select(func.count()).select_from(model))).scalar_one())
    return out


async def _reset_sequences(session) -> None:
    """PG：自增序列回 1（新赛季 audit_event.id 从 1 起）；SQLite：清 sqlite_sequence。"""
    dialect = session.bind.dialect.name
    for model in CLEAR_ORDER:
        t = model.__tablename__
        if dialect == "postgresql":
            await session.execute(text(
                f"SELECT setval(pg_get_serial_sequence('\"{t}\"', 'id'), 1, false) "
                f"WHERE pg_get_serial_sequence('\"{t}\"', 'id') IS NOT NULL"))
        elif dialect == "sqlite":
            # 只有 AUTOINCREMENT 表才有 sqlite_sequence；普通 rowid 表清空后自然从 1 起
            has = (await session.execute(text(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"))).first()
            if has:
                await session.execute(text("DELETE FROM sqlite_sequence WHERE name = :n"), {"n": t})


async def run(dry_run: bool) -> int:
    async with async_session_maker() as s:
        counts = await _counts(s)
        n_users = int((await s.execute(select(func.count()).select_from(User))).scalar_one())
        initial = await site_config.get_decimal(s, "initial_balance")

    print("将清空：")
    for t, n in counts.items():
        print(f"  {t:<26} {n:>8} 行")
    print(f"将重置 {n_users} 个用户：cash → {initial}，debt → 0，结息/强平时间戳 → NULL")
    print("保留：user / siteconfig / title* / user_title / redemption_partner|batch|code / alembic_version")
    if dry_run:
        print("\n[dry-run] 未做任何修改")
        return 0

    if input("\n确认执行赛季重置？输入 RESET: ").strip() != "RESET":
        print("已取消")
        return 1

    async with async_session_maker() as s:
        async with s.begin():
            # market.winning_outcome_id → outcome 的环形外键：先置空再删
            await s.execute(update(Market).values(winning_outcome_id=None))
            for model in CLEAR_ORDER:
                await s.execute(delete(model))
            await _reset_sequences(s)

            users = (await s.execute(select(User).order_by(User.id.asc()))).scalars().all()
            for u in users:
                u.cash = initial
                u.debt = Decimal("0")
                u.debt_last_accrued_at = None
                u.last_liquidated_at = None
                s.add(u)
                audit_service.record(
                    s, "user_register", user_id=u.id,
                    payload={"username": u.username, "is_superuser": u.is_superuser,
                             "source": "season_reset", "initial_balance": initial},
                    user_after=audit_service.user_snapshot(u),
                )
    print(f"\n已重置：清空 {sum(counts.values())} 行，{len(users)} 个用户现金还原到 {initial}")

    # 自检：事件流折叠应与线上表完全一致
    async with async_session_maker() as s:
        evs = await audit_replay.load_events(s)
        snap, mism = audit_replay.fold(evs, check=True)
        live = await audit_replay.compare_with_live(s, snap)
    if mism or live:
        print(f"⚠ 自检不一致 replay={len(mism)} live={len(live)}（见 scripts/audit_verify.py）")
        return 2
    print(f"自检 OK：events={len(evs)}，全员锚定")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    try:
        code = asyncio.run(run(args.dry_run))
    finally:
        asyncio.run(engine.dispose())
    sys.exit(code)
