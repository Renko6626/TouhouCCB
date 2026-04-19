from datetime import datetime
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
    market_value: Money       # 全部卖出可获得的 LMSR 清算价值（含滑点，已扣卖出手续费）
    unrealized_pnl: Money     # market_value - cost_basis（口径自洽，可与"卖出均价"配合）


class UserSummary(BaseModel):
    cash: Money
    debt: Money
    holdings_value: Money
    total_cost_basis: Money   # 所有持仓总成本
    unrealized_pnl: Money     # holdings_value - total_cost_basis
    net_worth: Money
    rank: str


class TransactionRead(BaseModel):
    id: int
    outcome_id: int
    type: str  # buy, sell, settle, settle_lose
    shares: Money
    price: Price
    gross: Money
    fee: Money
    cost: Money
    timestamp: datetime
