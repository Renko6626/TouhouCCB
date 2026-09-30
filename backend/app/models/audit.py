"""审计事件流（docs/superpowers/specs/2026-08-22-audit-events-design.md）。

每个改钱 / 改仓 / 改市场 / 改配置的操作，在**同一事务**内追加一条事件，
并带上操作后的实体快照。T 时刻状态 = 各实体 id ≤ cutoff 的最后一条事件的 *_after。
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Optional

from sqlmodel import SQLModel, Field
from sqlalchemy import BigInteger, Column, DateTime, Index, Integer, JSON


AUDIT_EVENT_TYPES = frozenset({
    "user_register",
    "trade_buy", "trade_sell", "trade_liquidate",
    "settle_win", "settle_lose",
    "market_create", "market_close", "market_resume", "market_settle",
    "loan_borrow", "loan_repay",
    "admin_adjust_cash", "admin_force_loan", "admin_forgive_debt", "admin_amnesty",
    "liquidation_repay", "liquidation",
    "interest_accrual",
    # ── 统一信贷风险（计划 §3.5）：新事件的 user_after 必须是**提交后**的权威快照；
    #    资金影响仍由同事务的 trade_liquidate / liquidation_repay / fx_trade 承担。
    #    ⚠️ 同一事务内必须先写资金事件、再写这些"记录型"事件：audit_replay.fold 对
    #    未知类型不校验增量但会把它当作新的 user 锚点，顺序反了会让后续资金事件对不上。 ──
    "liquidation_run_start", "liquidation_action", "liquidation_blocked",
    "liquidation_run_close", "credit_freeze_set",
    "admin_fx_short_writeoff",
    "admin_set_role", "admin_ban", "admin_unban",
    "config_set",
    "redeem_purchase", "danmuku_exchange",
    "redeem_fulfill", "redeem_fulfill_revoke",
    "fx_trade", "fx_fund", "fx_withdraw",
    "fx_event_publish", "fx_event_complete", "fx_event_cancel",
})


class AuditEvent(SQLModel, table=True):
    __tablename__ = "audit_event"
    __table_args__ = (
        Index("ix_audit_event_user_id_id", "user_id", "id"),
        Index("ix_audit_event_market_id_id", "market_id", "id"),
        Index("ix_audit_event_type_id", "event_type", "id"),
    )

    # BigInteger + autoincrement；SQLite 下 BigInteger 主键不自增，用 variant 退回 Integer
    id: Optional[int] = Field(
        default=None,
        sa_column=Column(BigInteger().with_variant(Integer(), "sqlite"),
                         primary_key=True, autoincrement=True),
    )
    ts: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_type=DateTime(timezone=True), index=True,
    )
    event_type: str = Field(max_length=32, index=True)

    user_id: Optional[int] = Field(default=None, index=True)
    market_id: Optional[int] = Field(default=None, index=True)
    outcome_id: Optional[int] = Field(default=None)
    operator_user_id: Optional[int] = Field(default=None)

    ref_table: Optional[str] = Field(default=None, max_length=32)
    ref_id: Optional[int] = Field(default=None)

    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    user_after: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON, nullable=True))
    position_after: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON, nullable=True))
    market_after: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON, nullable=True))
