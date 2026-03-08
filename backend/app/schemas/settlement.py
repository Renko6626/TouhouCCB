# app/schemas/settlement.py

from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field

class ResolveRequest(BaseModel):
    winning_outcome_id: int = Field(..., ge=1)
    # 可选：赢家兑付比例（默认 1.0）
    payout: float = Field(default=1.0, ge=0.0)

class SettleResult(BaseModel):
    market_id: int
    status: str
    winning_outcome_id: int
    settled_at: datetime
    total_payout: float
    settled_positions: int
