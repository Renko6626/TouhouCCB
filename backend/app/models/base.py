from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional, List
from sqlmodel import SQLModel, Field, Relationship
from pydantic import ConfigDict
from sqlalchemy import UniqueConstraint, CheckConstraint, Column, DateTime, ForeignKey, Index, Numeric, JSON


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
    cash: Decimal = Field(default=Decimal("500"), sa_type=Numeric(16, 6))
    debt: Decimal = Field(default=Decimal("0"), sa_type=Numeric(16, 6))

    # LoanV1 — 上次利息结算时间；debt=0 时为 None
    debt_last_accrued_at: Optional[datetime] = Field(
        default=None,
        sa_type=DateTime(timezone=True),
    )

    # 免责声明知情同意时间戳；为 null 表示用户尚未在 TosModal 勾选同意
    tos_accepted_at: Optional[datetime] = Field(
        default=None,
        sa_type=DateTime(timezone=True),
    )

    # 关系映射
    # ── lazy="raise_on_sql"（perf）──
    # 这两个反向集合在 hot path（buy/sell 的 _lock_user）里从不被使用，
    # 但配 "selectin" 会让每次 SELECT user FOR UPDATE 自动追加两条 SELECT
    # 把该用户的全部 positions/transactions 拖出来（活跃用户可能上千行）。
    # 改为 "raise_on_sql" 后：未显式预加载就访问会抛错，强制走显式 select(...).where(...)。
    positions: List["Position"] = Relationship(back_populates="user", sa_relationship_kwargs={"lazy": "raise_on_sql"})
    transactions: List["Transaction"] = Relationship(back_populates="user", sa_relationship_kwargs={"lazy": "raise_on_sql"})


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
    # ── lazy="raise_on_sql"（perf）──
    # 同 User：buy/sell 的 _lock_outcomes_for_market 每次会锁住该市场所有 outcome，
    # "selectin" 会让 ORM 自动追加 SELECT 把每个 outcome 的全部 positions/transactions
    # 拖出来（热门市场可能上万行）。hot path 不需要这些数据。
    positions: List["Position"] = Relationship(back_populates="outcome", sa_relationship_kwargs={"lazy": "raise_on_sql"})

    transactions: List["Transaction"] = Relationship(
        back_populates="outcome",
        sa_relationship_kwargs={"lazy": "raise_on_sql"}
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

    # 全市场所有 outcome 的 post 价快照（list[float]，按 outcome.id 升序）。
    # buy/sell 写入；settle/settle_lose 留 NULL；老历史数据由回填脚本补齐。
    # chart 接口读取范围内若全部非 NULL 则走 fast path，任一 NULL 则整段退回 NumPy replay。
    market_prices_post: Optional[List[float]] = Field(
        default=None,
        sa_column=Column(JSON, nullable=True),
    )

    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True, sa_type=DateTime(timezone=True))

    user: Optional["User"] = Relationship(back_populates="transactions")
    outcome: Optional["Outcome"] = Relationship(back_populates="transactions")


class OutcomeCandle(SQLModel, table=True):
    """物化 OHLCV K 线表。每笔 buy/sell 在事务内同步 UPSERT。

    自然键 (outcome_id, interval, bucket_start)：同一 bucket 多次成交
    INSERT ... ON CONFLICT DO UPDATE 合并 H/L/C/V/n。

    settle/settle_lose 不写入：结算价不是真实成交、且 timestamp 扎堆。

    不暴露 outcome relationship；Outcome 不加反向 candles 关系。
    理由：遵守 base.py:61-67 hot path 性能护栏（lazy="raise_on_sql" 精神）。
    """
    __tablename__ = "outcome_candle"
    __table_args__ = (
        CheckConstraint("volume_shares >= 0", name="ck_candle_volume_non_negative"),
        CheckConstraint("n_trades >= 0",      name="ck_candle_n_non_negative"),
        CheckConstraint("high_price >= low_price", name="ck_candle_h_ge_l"),
        CheckConstraint(
            "interval IN ('10s', '1m', '15m', '1h')",
            name="ck_candle_interval_supported",
        ),
    )

    outcome_id:   int      = Field(foreign_key="outcome.id", primary_key=True)
    interval:     str      = Field(primary_key=True, max_length=8)
    bucket_start: datetime = Field(primary_key=True, sa_type=DateTime(timezone=True))

    open_price:  Decimal = Field(sa_type=Numeric(16, 8))
    high_price:  Decimal = Field(sa_type=Numeric(16, 8))
    low_price:   Decimal = Field(sa_type=Numeric(16, 8))
    close_price: Decimal = Field(sa_type=Numeric(16, 8))

    volume_shares: Decimal = Field(default=Decimal("0"), sa_type=Numeric(16, 6))
    n_trades:      int     = Field(default=0)

    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_type=DateTime(timezone=True),
    )


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
