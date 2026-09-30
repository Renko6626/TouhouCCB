"""赛季重置：保留真人账号/称号/兑换核销，清活动数据和机器人，现金还原。

    python scripts/season_reset.py --dry-run     # 只打印将清理的行数与将重置的用户数
    python scripts/season_reset.py               # 交互输入 RESET 才执行；单事务

保留：user、siteconfig、title / title_code_batch / title_code / user_title、
      redemption_partner / redemption_batch / redemption_code（含已售出记录，作库存历史）、alembic_version
清空：market、outcome、position、transaction、outcome_candle、market_required_title、
      ledger_entry、liquidation_events、bot_suspicion、bot_profile，以及兑换以外的 audit_event。
兑换：redemption_transaction、danmuku_exchange 和全部兑换审计保留，避免丢掉已买到的激活码。
真人：cash = site_config.initial_balance，debt = 0，结息/强平时间戳清空；基本信息、账号状态和称号不变。
机器人：配置全部删除；没有保留记录引用的账号删除，否则停用且资产归零，以保留历史归属。
      pve_enabled 关闭。保留全部序列，PostgreSQL 的市场/选项 ID 不复用，避免旧 K 线缓存串季。
      每个保留账号重新写入 user_register(source=season_reset) 锚点。

**先备份**：docker compose exec -T postgres pg_dump -U thccb thccb > backups/thccb_pre_season_<ts>.sql
**先停后端**：docker compose stop backend（writer 内存状态 / 结息 sweep 不能与本脚本并发）
"""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import delete, func, or_, select, update  # noqa: E402
from sqlmodel import SQLModel  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

from app.core.database import async_session_maker, engine  # noqa: E402
import app.models.audit  # noqa: F401, E402
import app.models.ledger  # noqa: F401, E402
import app.models.redemption  # noqa: F401, E402
import app.models.fx  # noqa: F401, E402
import app.models.credit  # noqa: F401, E402
from app.models.audit import AuditEvent  # noqa: E402
from app.models.base import (  # noqa: E402
    BotSuspicion, LiquidationEvent, Market, Outcome, OutcomeCandle, Position, SiteConfig, Transaction, User,
)
from app.models.bot import BotProfile  # noqa: E402
from app.models.credit import LiquidationAction, LiquidationRun  # noqa: E402
from app.models.ledger import LedgerEntry  # noqa: E402
from app.models.fx import FxEvent, FxPair, FxTrade, FxTreasury, FxWallet, FxShortPosition  # noqa: E402
from app.models.title import MarketRequiredTitle  # noqa: E402
from app.services import audit_replay, audit_service, site_config  # noqa: E402
from app.services.credit.version import bump_economic_version  # noqa: E402

# 子表在前，父表在后（外键顺序）
# LiquidationAction.run_id → LiquidationRun.id：action 必须先删。
# LiquidationEvent.run_id → LiquidationRun.id 是 SET NULL，放前面只为顺序清晰。
CLEAR_ORDER = [
    AuditEvent, BotProfile, BotSuspicion,
    LiquidationAction, LiquidationEvent, LiquidationRun,
    LedgerEntry, Transaction, Position, OutcomeCandle,
    MarketRequiredTitle, Outcome, Market,
]

PRESERVED_AUDIT_TYPES = ("redeem_purchase", "danmuku_exchange", "redeem_fulfill", "redeem_fulfill_revoke")
FX_AUDIT_TYPES = (
    "fx_trade", "fx_fund", "fx_withdraw", "fx_event_publish", "fx_event_complete", "fx_event_cancel",
    "admin_fx_short_writeoff",
)
FX_CLEAR_ORDER = (FxShortPosition, FxWallet, FxTrade, FxEvent, FxTreasury, FxPair)
RESET_RULESET = "2026-09-27"


def _delete_statement(model):
    statement = delete(model)
    if model is AuditEvent:
        statement = statement.where(AuditEvent.event_type.not_in(PRESERVED_AUDIT_TYPES))
    return statement


async def _counts(session) -> dict[str, int]:
    out = {}
    for model in CLEAR_ORDER:
        query = select(func.count()).select_from(model)
        if model is AuditEvent:
            query = query.where(AuditEvent.event_type.not_in(PRESERVED_AUDIT_TYPES))
        out[model.__tablename__] = int((await session.execute(query)).scalar_one())
    return out


async def _referenced_bot_ids(session) -> set[int]:
    """保留记录引用的机器人不能硬删，否则破坏称号/兑换归属及管理员引用。"""
    bot_ids = select(User.id).where(User.is_bot.is_(True))
    cleared = {model.__tablename__ for model in CLEAR_ORDER}
    referenced = set()
    for table in SQLModel.metadata.tables.values():
        if table.name in cleared:
            continue
        for fk in table.foreign_keys:
            if fk.column.table.name == "user" and fk.column.name == "id":
                referenced.update((await session.execute(
                    select(fk.parent).where(fk.parent.in_(bot_ids))
                )).scalars().all())
    # audit_event 的账号字段没有 FK，但历史归属同样必须保留。
    for column in (AuditEvent.user_id, AuditEvent.operator_user_id):
        referenced.update((await session.execute(
            select(column).where(column.in_(bot_ids), AuditEvent.event_type.in_(PRESERVED_AUDIT_TYPES))
        )).scalars().all())
    return referenced


class ResetVerificationError(Exception):
    pass


async def reset_fx_state(session) -> None:
    """Remove all FX state and its audit trail within the caller's transaction."""
    await session.execute(delete(AuditEvent).where(or_(
        AuditEvent.event_type.in_(FX_AUDIT_TYPES),
        (AuditEvent.event_type == "interest_accrual")
        & (AuditEvent.ref_table == "fx_short_position"),
    )))
    for model in FX_CLEAR_ORDER:
        await session.execute(delete(model))
    for key in ("fx_enabled", "fx_short_enabled"):
        flag = (await session.execute(select(SiteConfig).where(SiteConfig.key == key))).scalar_one_or_none()
        if flag is None:
            flag = SiteConfig(key=key, value="false", value_type="bool")
        else:
            flag.value = "false"
            flag.updated_at = datetime.now(timezone.utc)
        session.add(flag)


async def run(dry_run: bool) -> int:
    async with async_session_maker() as s:
        counts = await _counts(s)
        for model in FX_CLEAR_ORDER:
            counts[model.__tablename__] = int((await s.execute(
                select(func.count()).select_from(model)
            )).scalar_one())
        n_users = int((await s.execute(select(func.count()).select_from(User).where(User.is_bot.is_(False)))).scalar_one())
        n_bots = int((await s.execute(select(func.count()).select_from(User).where(User.is_bot.is_(True)))).scalar_one())
        kept_bots = await _referenced_bot_ids(s)
        n_redemption_audits = int((await s.execute(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.event_type.in_(PRESERVED_AUDIT_TYPES))
        )).scalar_one())
        initial = await site_config.get_decimal(s, "initial_balance")
        if not initial.is_finite() or initial < 0:
            raise ValueError("initial_balance 必须是非负有限金额，已停止重置")

    print("将清空：")
    for t, n in counts.items():
        print(f"  {t:<26} {n:>8} 行")
    print(f"将重置 {n_users} 个真人用户：cash → {initial}，debt → 0，结息/强平时间戳 → NULL")
    print(f"机器人：删除 {n_bots - len(kept_bots)} 个无历史引用账号，停用并归零 {len(kept_bots)} 个历史引用账号，清除全部配置")
    print(f"保留兑换审计 {n_redemption_audits} 条；pve_enabled → false")
    print("保留：真人基本信息/账号状态/称号，siteconfig，title*，全部兑换码/购买/弹幕激活码/核销记录，alembic_version")
    print("不重置自增序列：生产 PostgreSQL 市场和选项 ID 继续递增，避免旧历史缓存串季")
    if dry_run:
        print("\n[dry-run] 未做任何修改")
        return 0

    if input("\n确认执行赛季重置？输入 RESET: ").strip() != "RESET":
        print("已取消")
        return 1

    try:
        async with async_session_maker() as s:
            async with s.begin():
                await reset_fx_state(s)
                # market.winning_outcome_id → outcome 的环形外键：先置空再删。
                await s.execute(update(Market).values(winning_outcome_id=None))
                for model in CLEAR_ORDER:
                    await s.execute(_delete_statement(model))
                referenced_bots = await _referenced_bot_ids(s)
                remove_bots = delete(User).where(User.is_bot.is_(True))
                if referenced_bots:
                    remove_bots = remove_bots.where(User.id.not_in(referenced_bots))
                await s.execute(remove_bots)

                pve = (await s.execute(select(SiteConfig).where(SiteConfig.key == "pve_enabled"))).scalar_one_or_none()
                if pve is None:
                    pve = SiteConfig(key="pve_enabled", value="false", value_type="bool")
                else:
                    pve.value = "false"
                    pve.updated_at = datetime.now(timezone.utc)
                s.add(pve)

                users = (await s.execute(select(User).order_by(User.id.asc()))).scalars().all()
                reset_humans = sum(not user.is_bot for user in users)
                retained_bots = sum(user.is_bot for user in users)
                for u in users:
                    u.cash = Decimal("0") if u.is_bot else initial
                    u.debt = Decimal("0")
                    u.debt_last_accrued_at = None
                    u.last_liquidated_at = None
                    # 统一信贷（WP1/WP6b）：新赛季从零开始，清坏账冻结并推进经济版本
                    # （现金/债务变了，旧快照一律作废）。run/action 已在 CLEAR_ORDER
                    # 里删除，新赛季不保留任何强平在途状态。
                    u.credit_frozen = False
                    bump_economic_version(u)
                    if u.is_bot:
                        u.is_active = False
                        u.is_superuser = False
                    s.add(u)
                    audit_service.record(
                        s, "user_register", user_id=u.id,
                        payload={"username": u.username, "is_superuser": u.is_superuser,
                                 "source": "season_reset", "initial_balance": u.cash,
                                 "is_bot": u.is_bot},
                        user_after=audit_service.user_snapshot(u),
                    )
                await s.flush()
                # 提交之前自检，失败会回滚整次重置；不调用非事务性的 setval。
                evs = await audit_replay.load_events(s)
                snap, mism = audit_replay.fold(evs, check=True)
                live = await audit_replay.compare_with_live(s, snap)
                if mism or live:
                    raise ResetVerificationError(f"replay={len(mism)} live={len(live)}")
    except ResetVerificationError as exc:
        print(f"自检失败，全部重置已回滚：{exc}")
        return 2
    site_config.clear_cache()
    print(f"\n已重置：清空 {sum(counts.values())} 行，{reset_humans} 个真人现金还原到 {initial}，{retained_bots} 个历史机器人账号停用归零")
    print(f"自检 OK：events={len(evs)}，全员锚定")
    return 0


async def _main(dry_run: bool) -> int:
    loop = asyncio.get_running_loop()
    current = asyncio.current_task()
    stopping = False

    def terminate() -> None:
        nonlocal stopping
        if not stopping:
            stopping = True
            current.cancel()

    # 作为容器 PID 1 时不能依赖 SIGTERM 默认行为；取消任务会先退出事务并回滚。
    loop.add_signal_handler(signal.SIGTERM, terminate)
    try:
        return await run(dry_run)
    except asyncio.CancelledError:
        print("收到终止信号，已停止运行；未提交事务已退出，请核对预览和备份")
        return 143
    finally:
        # asyncpg 连接必须在创建它的事件循环尚存活时关闭。
        await engine.dispose()
        loop.remove_signal_handler(signal.SIGTERM)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--expected-ruleset", choices=[RESET_RULESET], help="运维入口用此参数拒绝执行不兼容的旧镜像")
    args = ap.parse_args()
    sys.exit(asyncio.run(_main(args.dry_run)))
