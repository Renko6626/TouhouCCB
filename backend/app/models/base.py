from datetime import datetime
from typing import Optional, List
from sqlmodel import SQLModel, Field, Relationship
from pydantic import ConfigDict
from sqlalchemy import UniqueConstraint, Column, ForeignKey
class User(SQLModel, table=True):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    # FastAPI-Users 核心字段
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(sa_column_kwargs={"unique": True, "index": True}, nullable=False)
    hashed_password: str = Field(nullable=False)
    is_active: bool = Field(default=True, nullable=False)
    is_superuser: bool = Field(default=False, nullable=False)
    is_verified: bool = Field(default=False, nullable=False)

    # 业务属性
    username: str = Field(unique=True, index=True, nullable=False)
    cash: float = Field(default=100.0)
    debt: float = Field(default=0.0)

    # 关系映射
    positions: List["Position"] = Relationship(back_populates="user", sa_relationship_kwargs={"lazy": "selectin"})
    transactions: List["Transaction"] = Relationship(back_populates="user", sa_relationship_kwargs={"lazy": "selectin"})

class Market(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str = Field(index=True)
    description: str = ""
    liquidity_b: float = Field(default=100.0)
    status: str = Field(default="trading") # trading, halt, settled
    created_at: datetime = Field(default_factory=datetime.now)

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
    settled_at: Optional[datetime] = Field(default=None, index=True)
    settled_by_user_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)

class Outcome(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    market_id: int = Field(
        foreign_key="market.id",
        index=True,
    )
    label: str
    total_shares: float = Field(default=0.0)

    # ✅ 可选：结算兑付（结算后写入：winner=1.0, loser=0.0）
    payout: Optional[float] = Field(default=None)
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
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    outcome_id: int = Field(foreign_key="outcome.id", index=True)
    amount: float = Field(default=0.0)

    user: Optional["User"] = Relationship(back_populates="positions")
    outcome: Optional["Outcome"] = Relationship(back_populates="positions")

class Transaction(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    user_id: int = Field(foreign_key="user.id", index=True)

    # ✅ 必须：归属标的（哪一个 Outcome）
    outcome_id: int = Field(foreign_key="outcome.id", index=True)

    type: str  # buy, sell, settle
    shares: float

    # ✅ 净现金流：buy 为 +支出；sell 为 -收入（你原先逻辑）
    cost: float

    # ✅ 手续费（没有就 0）
    fee: float = Field(default=0.0)

    # ✅ 手续费前的“绝对交易额”（buy/sell 都为正数）
    gross: float = Field(default=0.0)

    # ✅ 手续费前的成交单价（K线推荐用这个）
    price: float = Field(default=0.0)

    timestamp: datetime = Field(default_factory=datetime.utcnow, index=True)

    user: Optional["User"] = Relationship(back_populates="transactions")
    outcome: Optional["Outcome"] = Relationship(back_populates="transactions")
