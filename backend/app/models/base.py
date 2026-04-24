from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional, List
from sqlmodel import SQLModel, Field, Relationship
from pydantic import ConfigDict
from sqlalchemy import UniqueConstraint, CheckConstraint, Column, DateTime, ForeignKey, Index, Numeric


class MarketStatus(str, Enum):
    TRADING = "trading"
    HALT = "halt"
    SETTLED = "settled"


class TransactionType(str, Enum):
    BUY = "buy"
    SELL = "sell"
    SETTLE = "settle"
    SETTLE_LOSE = "settle_lose"


class User(SQLModel, table=True):
    __table_args__ = (
        CheckConstraint("cash >= 0", name="ck_user_cash_non_negative"),
        CheckConstraint("debt >= 0", name="ck_user_debt_non_negative"),
    )

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: Optional[int] = Field(default=None, primary_key=True)

    # Casdoor SSO
    casdoor_id: Optional[str] = Field(default=None, sa_column_kwargs={"unique": True, "index": True})

    # 从 Casdoor 同步的基本信息
    username: str = Field(unique=True, index=True, nullable=False)
    email: Optional[str] = Field(default=None, sa_column_kwargs={"unique": True, "index": True})

    # 账号状态
    is_active: bool = Field(default=True, nullable=False)
    is_superuser: bool = Field(default=False, nullable=False)

    # 游戏业务属性 — Decimal(16,6)
    cash: Decimal = Field(default=Decimal("100"), sa_type=Numeric(16, 6))
    debt: Decimal = Field(default=Decimal("0"), sa_type=Numeric(16, 6))

    # LoanV1 — 上次利息结算时间；debt=0 时为 None
    debt_last_accrued_at: Optional[datetime] = Field(
        default=None,
        sa_type=DateTime(timezone=True),
    )

    # 关系映射
    positions: List["Position"] = Relationship(back_populates="user", sa_relationship_kwargs={"lazy": "selectin"})
    transactions: List["Transaction"] = Relationship(back_populates="user", sa_relationship_kwargs={"lazy": "selectin"})


class Market(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str = Field(index=True)
    description: str = ""
    liquidity_b: float = Field(default=100.0)  # LMSR 参数，保持 float
    status: str = Field(default=MarketStatus.TRADING)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), sa_type=DateTime(timezone=True))
    closes_at: Optional[datetime] = Field(default=None, index=True, sa_type=DateTime(timezone=True))
    tags: str = Field(default="")

    outcomes: List["Outcome"] = Relationship(
        back_populates="market",
        sa_relationship_kwargs={
            "foreign_keys": "Outcome.market_id"
        }
    )

    winning_outcome_id: Optional[int] = Field(
        default=None,
        sa_column=Column(
            ForeignKey(
                "outcome.id",
                name="fk_market_winning_outcome",
                use_alter=True,
            ),
            index=True,
        ),
    )

    winning_outcome: Optional["Outcome"] = Relationship(
        sa_relationship_kwargs={
            "foreign_keys": "Market.winning_outcome_id"
        }
    )
    settled_at: Optional[datetime] = Field(default=None, index=True, sa_type=DateTime(timezone=True))
    settled_by_user_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)


class Outcome(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    market_id: int = Field(
        foreign_key="market.id",
        index=True,
    )
    label: str
    total_shares: Decimal = Field(default=Decimal("0"), sa_type=Numeric(16, 6))

    payout: Optional[Decimal] = Field(default=None, sa_type=Numeric(16, 8))
    market: Optional["Market"] = Relationship(
        back_populates="outcomes",
        sa_relationship_kwargs={
            "foreign_keys": "Outcome.market_id"
        }
    )
    positions: List["Position"] = Relationship(back_populates="outcome", sa_relationship_kwargs={"lazy": "selectin"})

    transactions: List["Transaction"] = Relationship(
        back_populates="outcome",
        sa_relationship_kwargs={"lazy": "selectin"}
    )


class Position(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("user_id", "outcome_id", name="uq_position_user_outcome"),
        CheckConstraint("amount >= 0", name="ck_position_amount_non_negative"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    outcome_id: int = Field(foreign_key="outcome.id", index=True)
    amount: Decimal = Field(default=Decimal("0"), sa_type=Numeric(16, 6))
    cost_basis: Decimal = Field(default=Decimal("0"), sa_type=Numeric(16, 6))  # 持仓总成本

    user: Optional["User"] = Relationship(back_populates="positions")
    outcome: Optional["Outcome"] = Relationship(back_populates="positions")


class Transaction(SQLModel, table=True):
    __table_args__ = (
        Index("ix_transaction_outcome_timestamp", "outcome_id", "timestamp"),
        Index("ix_transaction_user_timestamp", "user_id", "timestamp"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)

    user_id: int = Field(foreign_key="user.id", index=True)
    outcome_id: int = Field(foreign_key="outcome.id", index=True)

    type: str  # TransactionType enum value
    shares: Decimal = Field(sa_type=Numeric(16, 6))

    # 净现金流：buy 为 +支出；sell 为 -收入
    cost: Decimal = Field(sa_type=Numeric(16, 6))

    # 手续费
    fee: Decimal = Field(default=Decimal("0"), sa_type=Numeric(16, 6))

    # 手续费前的绝对交易额
    gross: Decimal = Field(default=Decimal("0"), sa_type=Numeric(16, 6))

    # 手续费前的成交单价 — 8位精度
    price: Decimal = Field(default=Decimal("0"), sa_type=Numeric(16, 8))

    # 交易前/后该选项的瞬时市场价（K线用）
    pre_market_price: Decimal = Field(default=Decimal("0"), sa_type=Numeric(16, 8))
    post_market_price: Decimal = Field(default=Decimal("0"), sa_type=Numeric(16, 8))

    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True, sa_type=DateTime(timezone=True))

    user: Optional["User"] = Relationship(back_populates="transactions")
    outcome: Optional["Outcome"] = Relationship(back_populates="transactions")


class SiteConfig(SQLModel, table=True):
    """站点 key-value 配置表，超管可运行时修改。"""
    __table_args__ = (
        UniqueConstraint("key", name="uq_siteconfig_key"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    key: str = Field(index=True, nullable=False)
    value: str = Field(nullable=False)
    value_type: str = Field(nullable=False)  # "decimal" | "int" | "bool"
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_type=DateTime(timezone=True),
    )
    updated_by: Optional[int] = Field(default=None, foreign_key="user.id")
