"""统一信贷风险执行记录（spec 2026-09-29 §2.1 / 计划 §3.3）。

只持久化执行正确性所需的两张表：

- ``LiquidationRun``：每用户最多一个 ``status='active'`` 的强平轮次（部分唯一索引），
  记录本轮起点快照与累计成交，供崩溃恢复 / 下一轮扫描续接与审计。
- ``LiquidationAction``：每轮每个实际动作的幂等记录，``(run_id, round_no)`` 唯一，
  防止重复卖仓 / 重复还债。

资金影响不在这里：现金、债务仍由 ``User.cash/debt`` 与 ``ledger_entry`` 承担，
本表只做执行记录（``user_after`` 审计快照口径见 app.services.audit_service）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    JSON,
    Numeric,
    UniqueConstraint,
    text,
)
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# 冻结取值（credits §3.3）：run 终态与 action 种类。DDL 里以 CheckConstraint 固化，
# 后续 WP 不得再改；若必须新增取值，回 WP1 走一次 additive revision。
RUN_STATUSES = ("active", "recovered", "insolvent", "blocked", "stopped")
ACTION_KINDS = ("sell_group", "repay_cash", "repay_only", "blocked", "stopped")
FEE_CURRENCIES = ("gold", "foreign")


class LiquidationRun(SQLModel, table=True):
    """一次强平"轮次"（跨多次定时扫描续接，直到恢复 / 资不抵债 / 阻塞 / 停止）。"""

    __tablename__ = "liquidation_run"
    __table_args__ = (
        # 每用户最多一个活跃 run；recovered/insolvent/blocked/stopped 后可以再建。
        # 部分唯一索引：PostgreSQL 与 SQLite 都支持 WHERE 子句（MySQL 不支持，
        # 但本项目不跑 MySQL 迁移，见 docs/migrations.md 驱动注意）。
        Index(
            "uq_liquidation_run_active_user",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
        Index("ix_liquidation_run_status_updated", "status", "updated_at"),
        CheckConstraint(
            "status IN ('active','recovered','insolvent','blocked','stopped')",
            name="ck_liquidation_run_status",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", nullable=False, index=True)
    status: str = Field(default="active", max_length=16, nullable=False)
    trigger_source: str = Field(max_length=32, nullable=False)
    started_at: datetime = Field(
        default_factory=_utcnow, sa_type=DateTime(timezone=True), nullable=False
    )
    updated_at: datetime = Field(
        default_factory=_utcnow, sa_type=DateTime(timezone=True), nullable=False
    )
    # 下一轮编号（从 1 开始）与本 run 已提交的动作数。
    next_round: int = Field(default=1, nullable=False)
    rounds: int = Field(default=0, nullable=False)
    last_group_product: Optional[str] = Field(default=None, max_length=8)
    last_group_id: Optional[int] = Field(default=None)
    last_blocked_reason: Optional[str] = Field(default=None, max_length=255)
    closed_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))

    # run 起点快照（只读参考，不参与判定；判定永远用当轮实时值）。
    pre_cash: Decimal = Field(default=Decimal("0"), sa_type=Numeric(16, 6), nullable=False)
    pre_debt: Decimal = Field(default=Decimal("0"), sa_type=Numeric(16, 6), nullable=False)
    pre_liquidation_equity: Decimal = Field(
        default=Decimal("0"), sa_type=Numeric(16, 6), nullable=False
    )
    total_proceeds: Decimal = Field(default=Decimal("0"), sa_type=Numeric(16, 6), nullable=False)
    total_repaid: Decimal = Field(default=Decimal("0"), sa_type=Numeric(16, 6), nullable=False)
    total_fee: Decimal = Field(default=Decimal("0"), sa_type=Numeric(16, 6), nullable=False)


class LiquidationAction(SQLModel, table=True):
    """强平轮次里的一个已提交动作（每轮最多一个；``(run_id, round_no)`` 唯一）。"""

    __tablename__ = "liquidation_action"
    __table_args__ = (
        UniqueConstraint("run_id", "round_no", name="uq_liquidation_action_run_round"),
        Index("ix_liquidation_action_user_created", "user_id", "created_at"),
        CheckConstraint(
            "kind IN ('sell_group','repay_cash','repay_only','blocked','stopped')",
            name="ck_liquidation_action_kind",
        ),
        CheckConstraint(
            "fee_currency IS NULL OR fee_currency IN ('gold','foreign')",
            name="ck_liquidation_action_fee_currency",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    # 动作从属于 run：run 被删时动作一并删除（赛季重置显式按序删，不依赖 CASCADE）。
    run_id: int = Field(
        sa_column=Column(
            ForeignKey("liquidation_run.id", ondelete="CASCADE"), nullable=False, index=True
        )
    )
    user_id: int = Field(foreign_key="user.id", nullable=False, index=True)
    round_no: int = Field(nullable=False)
    kind: str = Field(max_length=16, nullable=False)
    product: Optional[str] = Field(default=None, max_length=8)
    group_id: Optional[int] = Field(default=None)
    mode: Optional[str] = Field(default=None, max_length=8)
    requested: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON, nullable=True))
    executed: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON, nullable=True))
    proceeds: Decimal = Field(default=Decimal("0"), sa_type=Numeric(16, 6), nullable=False)
    fee: Decimal = Field(default=Decimal("0"), sa_type=Numeric(16, 6), nullable=False)
    fee_currency: Optional[str] = Field(default=None, max_length=8)
    repaid: Decimal = Field(default=Decimal("0"), sa_type=Numeric(16, 6), nullable=False)
    debt_after: Decimal = Field(default=Decimal("0"), sa_type=Numeric(16, 6), nullable=False)
    cash_after: Decimal = Field(default=Decimal("0"), sa_type=Numeric(16, 6), nullable=False)
    economic_version_after: int = Field(default=0, nullable=False)
    blocked_reason: Optional[str] = Field(default=None, max_length=255)
    created_at: datetime = Field(
        default_factory=_utcnow, sa_type=DateTime(timezone=True), nullable=False
    )
