from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel

from app.schemas.base import Money, Price
from app.schemas.title import TitleChipRead


class HoldingRead(BaseModel):
    market_id: int
    market_title: str
    outcome_id: int
    outcome_label: str
    amount: Money
    cost_basis: Money
    avg_price: Price          # cost_basis / amount，真实买入加权均价
    current_price: Price      # LMSR 边际价（再买/再卖第 1 份的瞬时价）
    market_value: Money       # 全部卖出可获得的 LMSR 清算价值（LCV，含滑点+扣 sell_fee）
    # MTM 主显示口径：amount × current_price - cost_basis（账面浮盈，不含滑点）
    # 与 UserSummary.unrealized_pnl / net_worth 保持同口径，让顶部资产卡 ≡ 表格求和
    unrealized_pnl: Money
    # LCV 保守口径：market_value - cost_basis（立即变现浮盈，含滑点+扣 fee）
    # 与"卖出均价 = market_value / amount"自洽：amount × (sell_avg - avg_price) = unrealized_pnl_liquidation
    unrealized_pnl_liquidation: Money


class UserSummary(BaseModel):
    cash: Money
    debt: Money
    # 主显示口径（MTM 瞬时价 × 数量，不含滑点，符合用户对"我有多少钱"的直觉）
    holdings_value: Money
    # 保守口径（LCV 立即清算价值，含滑点 + 扣 sell_fee，强平/借款额度按这个）
    # 大仓位用户 LCV 通常 < MTM；UI 提示用户两者差距以理解 LMSR 滑点
    holdings_value_liquidation: Money
    total_cost_basis: Money   # 所有持仓总成本
    unrealized_pnl: Money     # MTM 主显示：holdings_value (MTM) - total_cost_basis
    unrealized_pnl_liquidation: Money  # LCV 保守：holdings_value_liquidation - total_cost_basis
    net_worth: Money          # cash - debt + holdings_value (MTM)，显示/排名用
    net_worth_liquidation: Money  # cash - debt + holdings_value_liquidation，margin 用
    rank: str                 # 按 net_worth (MTM) 算
    # margin 按 net_worth_liquidation / debt 算，比 MTM 更保守
    # 用 Optional[Money] / Money 确保 JSON 序列化为 number 而非 string
    # （之前用 raw Optional[Decimal] / Decimal，pydantic v2 默认序列化 Decimal
    # 为 JSON string，前端 TS 类型标 number 实际拿到 string → MarginCallBanner
    # 内 ratio.toFixed() / hardThr.toFixed() 在大持仓 + 借款 + LCV 负净值场景
    # 触发 "TypeError: a.toFixed is not a function"）
    margin_ratio: Optional[Money] = None
    margin_status: str = "healthy"
    last_liquidated_at: Optional[datetime] = None
    margin_hard_threshold: Money = Decimal("0.2")
    margin_soft_threshold: Money = Decimal("0.5")
    # 用户在 HALT 市场有持仓 → sweep 会跳过强平。前端把 danger banner 换成 info 状态。
    liquidation_protected: bool = False
    # Title 系统：当前佩戴的 title (chip) + 全部持有 title 列表（前端 chip 渲染 + MyTitlesPanel）
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
