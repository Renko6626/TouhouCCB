from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
from fastapi_users import schemas


# --- 市场相关 ---
class OutcomeRead(BaseModel):
    id: int
    label: str
    total_shares: float
    # 当前价格由后端计算后填入
    current_price: Optional[float] = None

class MarketRead(BaseModel):
    id: int
    title: str
    status: str
    liquidity_b: float
    outcomes: List[OutcomeRead]

# --- 交易相关 ---
class TransactionRead(BaseModel):
    id: int
    type: str
    shares: float
    price: float
    cost: float
    timestamp: datetime