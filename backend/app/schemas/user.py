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


class FxWalletSummary(BaseModel):
    pair_id: int
    currency_code: str
    currency_name: str = ""
    cost_basis: Money = Decimal("0")
    foreign_amount: Money
    mtm_gold: Money


class UserSummary(BaseModel):
    """账户快照；统一模式的清算指标由服务端按产品真实报价计算。

    客户端仍可续写 MTM 价格，但不能将它当作最新风险准入结论。
    关闭统一信贷时保留旧字段与无债零 LCV 路径。
    """
    cash: Money
    debt: Money
    # New valuation fields are populated only with unified credit enabled.
    fx_wallets: List[FxWalletSummary] = []
    display_equity: Optional[Money] = None
    liquidation_equity: Optional[Money] = None
    debt_with_interest: Optional[Money] = None
    credit_leverage: Optional[float] = None
    r_initial: Optional[float] = None
    r_maintenance: Optional[float] = None
    equity_to_debt: Optional[float] = None
    risk_status: Optional[str] = None
    credit_frozen: bool = False
    unified_credit_enabled: bool = False
    fx_mtm: Money = Decimal("0")
    fx_cost_basis: Money = Decimal("0")
    fx_unrealized_pnl: Money = Decimal("0")
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
