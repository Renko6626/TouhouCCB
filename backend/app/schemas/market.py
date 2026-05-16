from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field

from app.schemas.base import Money, Price


class MarketCreate(BaseModel):
    title: str
    description: Optional[str] = ""
    liquidity_b: float = Field(default=100.0, gt=0)
    outcomes: List[str] = Field(..., min_length=2, description="至少提供两个选项名称")
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
    # ── 滑点保护（P1）──
    # 客户端可以传绝对上下限（优先），或百分比（万分之一，bps）。
    # 都不传时服务端用 DEFAULT_SLIPPAGE_BPS=500（5%）兜底，且 HARDCAP_SLIPPAGE_BPS=1000（10%）截断。
    max_cost: Optional[Decimal] = Field(
        default=None,
        gt=0,
        description="买入最高可接受成本（含费）；为空则用 max_slippage_bps",
    )
    min_proceeds: Optional[Decimal] = Field(
        default=None,
        gt=0,
        description="卖出最低可接受收入（净）；为空则用 max_slippage_bps",
    )
    max_slippage_bps: Optional[int] = Field(
        default=None,
        ge=0,
        le=10000,
        description="最大滑点（万分之一），默认 500=5%；服务端 hardcap=1000=10%。"
                    "accept_any_slippage=True 时此字段被忽略",
    )
    accept_any_slippage: bool = Field(
        default=False,
        description="True 时跳过 max_slippage_bps 检查（仍检查 max_cost/min_proceeds）。"
                    "供平仓/大额建仓等用户明确接受任意滑点的场景；UI 应清晰标识。",
    )


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
    # 排序分值；net_worth 模式 = cash - debt + 持仓 LMSR 清算价，spending 模式 = 兑换消费总额 - 当前债务
    net_worth: Money
    rank: str
    # spending 模式额外填充：累计兑换消费 & 当前债务
    spent_total: Money | None = None
    debt: Money | None = None


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
