from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class MarketCreate(BaseModel):
    title: str
    description: Optional[str] = ""
    liquidity_b: float = Field(default=100.0, gt=0)
    outcomes: List[str] = Field(..., min_items=2, description="至少提供两个选项名称")


class OutcomePriceRead(BaseModel):
    id: int
    label: str
    shares: float
    current_price: float


class MarketListItem(BaseModel):
    id: int
    title: str
    description: Optional[str] = ""
    liquidity_b: float
    status: str
    outcomes: List[OutcomePriceRead]


class TradeRequest(BaseModel):
    outcome_id: int
    shares: float = Field(..., gt=0)


class TradeResponse(BaseModel):
    shares: float
    cost: float
    new_cash: float
    message: str


class SettleRequest(BaseModel):
    winning_outcome_id: int


class ResolveRequest(BaseModel):
    winning_outcome_id: int
    payout: float = Field(..., ge=0)


class SettleResult(BaseModel):
    market_id: int
    status: str
    winning_outcome_id: int
    settled_at: datetime
    total_payout: float
    settled_positions: int


class OutcomeQuoteRead(BaseModel):
    id: int
    label: str
    total_shares: float
    current_price: float
    payout: Optional[float] = None
    is_winner: Optional[bool] = None


class MarketDetailRead(BaseModel):
    id: int
    title: str
    description: str
    status: str
    liquidity_b: float
    created_at: datetime

    winning_outcome_id: Optional[int] = None
    settled_at: Optional[datetime] = None
    settled_by_user_id: Optional[int] = None

    outcomes: List[OutcomeQuoteRead]
    last_trade_at: Optional[datetime] = None
# ===== 新增 =====

class QuoteRequest(BaseModel):
    outcome_id: int
    shares: float = Field(..., gt=0)
    side: str = Field(..., pattern="^(buy|sell)$")


class QuoteResponse(BaseModel):
    outcome_id: int
    side: str
    shares: float
    avg_price: float
    gross: float        # 手续费前总额
    fee: float
    net: float          # 实际支付/获得
    after_prices: List[OutcomePriceRead]


class MarketTradeRead(BaseModel):
    id: int
    outcome_id: int
    side: str
    shares: float
    price: float
    gross: float
    fee: float
    timestamp: datetime


class LeaderboardItem(BaseModel):
    user_id: int
    username: str
    net_worth: float
    rank: str
