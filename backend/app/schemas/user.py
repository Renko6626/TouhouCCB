from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel

from app.schemas.base import Money, Price
from app.schemas.title import TitleChipRead


class HoldingRead(BaseModel):
    """阶段 3 瘦身（spec §6.4）：只有标签 + 数量/成本（6dp）。
    估值列（现价/市值/浮盈/均价）由客户端 utils/lmsr.ts 本地算。"""
    market_id: int
    market_title: str
    outcome_id: int
    outcome_label: str
    amount: Money        # 6dp（原 2dp 展示口径废弃——是客户端估值输入）
    cost_basis: Money    # 6dp


class SummaryPosition(BaseModel):
    outcome_id: int
    market_id: int
    amount: Money        # 6dp
    cost_basis: Money    # 6dp


class RankThresholdItem(BaseModel):
    """rank 阈值档；min_net_worth=None 是兜底档。客户端判定规则：
    命中第一个「min_net_worth is None 或 net_worth > min_net_worth」的条目。"""
    min_net_worth: Optional[Money] = None
    title: str


class UserSummary(BaseModel):
    """阶段 3 新契约（spec §6.4）：只返回客户端算不出来的东西。

    holdings_value / net_worth / unrealized_pnl / rank / margin_ratio 等
    派生值由前端 utils/lmsr.ts + priceContext 本地算；margin_status 仍是
    服务端权威（LCV 口径，spec §6.3——真正触发强平的是 sweep）。
    cash 是客户端成交后本地 apply 的基线，6dp 全精度。
    """
    cash: Money
    debt: Money
    positions: List[SummaryPosition] = []
    margin_hard_threshold: Money = Decimal("0.2")
    margin_soft_threshold: Money = Decimal("0.5")
    sell_fee_rate: Money = Decimal("0")
    rank_thresholds: List[RankThresholdItem] = []
    margin_status: str = "healthy"
    liquidation_protected: bool = False
    last_liquidated_at: Optional[datetime] = None
    equipped_title: Optional[TitleChipRead] = None
    all_titles: List["UserSummaryTitleItem"] = []


class UserSummaryTitleItem(BaseModel):
    """UserSummary.all_titles 项 — 比 chip 多带 description/sort_order，
    给前端 MyTitlesPanel 列表渲染使用。"""
    id: int
    name: str
    color: str
    icon: str
    description: str
    sort_order: int


UserSummary.model_rebuild()


class TransactionRead(BaseModel):
    id: int
    outcome_id: int
    market_id: Optional[int] = None
    market_title: Optional[str] = None
    outcome_label: Optional[str] = None
    type: str  # buy, sell, settle, settle_lose
    shares: Money
    price: Price
    gross: Money
    fee: Money
    cost: Money
    timestamp: datetime
