from decimal import Decimal
from typing import Annotated, List, Optional
from datetime import datetime

from pydantic import BaseModel
from pydantic.functional_serializers import PlainSerializer

# Decimal 字段在 JSON 序列化时输出为 number 而非 string
Money = Annotated[Decimal, PlainSerializer(lambda v: float(v), return_type=float)]
Price = Annotated[Decimal, PlainSerializer(lambda v: float(v), return_type=float)]


# --- 市场相关 ---
class OutcomeRead(BaseModel):
    id: int
    label: str
    total_shares: Money
    current_price: Optional[Price] = None

class MarketRead(BaseModel):
    id: int
    title: str
    status: str
    liquidity_b: float
    outcomes: List[OutcomeRead]
