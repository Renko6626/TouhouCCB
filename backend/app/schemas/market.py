from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field

from app.schemas.base import Money, Price


class MarketCreate(BaseModel):
    title: str
    description: Optional[str] = ""
    liquidity_b: float = Field(default=100.0, gt=0)
    outcomes: List[str] = Field(..., min_items=2, description="至少提供两个选项名称")
    closes_at: Optional[datetime] = Field(default=None, description="交易截止时间（UTC），到期后自动禁止交易")
    tags: Optional[List[str]] = Field(default=None, description="分类标签列表")


class OutcomePriceRead(BaseModel):
    id: int
    label: str
    shares: float
    current_price: float
    price_change_24h: Optional[float] = None
    price_change_pct_24h: Optional[float] = None


class MarketListItem(BaseModel):
    id: int
    title: str
    description: Optional[str] = ""
    liquidity_b: float
    status: str
    outcomes: List[OutcomePriceRead]
    trade_count: Optional[int] = 0
    last_trade_at: Optional[datetime] = None


class TradeRequest(BaseModel):
    outcome_id: int
    shares: Decimal = Field(..., gt=0)


class TradeResponse(BaseModel):
    shares: float
    cost: float
    new_cash: float
    message: str


class ResolveRequest(BaseModel):
    winning_outcome_id: int
    payout: Decimal = Field(..., ge=0)


class SettleResult(BaseModel):
    market_id: int
    status: str
    winning_outcome_id: int
    settled_at: datetime
    total_payout: Money
    settled_positions: int


class OutcomeQuoteRead(BaseModel):
    id: int
    label: str
    total_shares: Money
    current_price: Price
    payout: Optional[Money] = None
    is_winner: Optional[bool] = None
    price_change_24h: Optional[float] = None
    price_change_pct_24h: Optional[float] = None


class MarketDetailRead(BaseModel):
    id: int
    title: str
    description: str
    status: str
    liquidity_b: float
    created_at: datetime
    closes_at: Optional[datetime] = None
    tags: List[str] = []

    winning_outcome_id: Optional[int] = None
    settled_at: Optional[datetime] = None
    settled_by_user_id: Optional[int] = None

    outcomes: List[OutcomeQuoteRead]
    last_trade_at: Optional[datetime] = None


class QuoteRequest(BaseModel):
    outcome_id: int
    shares: Decimal = Field(..., gt=0)
    side: str = Field(..., pattern="^(buy|sell)$")


class QuoteResponse(BaseModel):
    outcome_id: int
    side: str
    shares: Money
    avg_price: Price
    gross: Money
    fee: Money
    net: Money
    after_prices: List[OutcomePriceRead]


class MarketTradeRead(BaseModel):
    id: int
    outcome_id: int
    side: str
    shares: Money
    price: Price
    gross: Money
    fee: Money
    timestamp: datetime
    username: str


class LeaderboardItem(BaseModel):
    user_id: int
    username: str
    net_worth: Money
    rank: str


class RecentTradeRead(BaseModel):
    """跨市场最近成交（用于首页实时成交流）"""
    id: int
    timestamp: datetime
    market_id: int
    market_title: str
    outcome_id: int
    outcome_label: str
    type: str            # 'buy' | 'sell'
    shares: Money
    price: Price
    username: str


class MoverItem(BaseModel):
    """涨跌榜单项（用于首页价格变动榜）"""
    market_id: int
    market_title: str
    outcome_id: int
    outcome_label: str
    price_now: Price
    price_then: Price
    change_pct: float    # (price_now - price_then) / price_then * 100，可正可负
