from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel

from app.schemas.base import Money, Price


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
    margin_ratio: Optional[Decimal] = None
    margin_status: str = "healthy"
    last_liquidated_at: Optional[datetime] = None
    margin_hard_threshold: Decimal = Decimal("0.2")
    margin_soft_threshold: Decimal = Decimal("0.5")
    # 用户在 HALT 市场有持仓 → sweep 会跳过强平。前端把 danger banner 换成 info 状态。
    liquidation_protected: bool = False


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
