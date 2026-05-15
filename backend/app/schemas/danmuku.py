"""弹幕兑换接口 Schema。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import List

from pydantic import BaseModel, Field


class DanmukuExchangeRequest(BaseModel):
    qq_user_id: str = Field(..., min_length=1, max_length=32, description="弹幕系统侧的 user_id（QQ 号）")
    room_id: str = Field(default="弹幕群", min_length=1, max_length=64)
    yuan: Decimal = Field(default=Decimal("0"), ge=0)
    huo: Decimal = Field(default=Decimal("0"), ge=0)


class DanmukuExchangeResponse(BaseModel):
    id: int
    code_string: str
    yuan: Decimal
    huo: Decimal
    amount: Decimal  # 实际扣减的站内 cash = yuan + huo（1:1）
    cash_after: Decimal  # 兑换后余额
    timestamp: datetime


class DanmukuExchangeHistoryItem(BaseModel):
    id: int
    qq_user_id: str
    room_id: str
    yuan: Decimal
    huo: Decimal
    amount: Decimal
    code_string: str
    timestamp: datetime


class DanmukuExchangeHistoryResponse(BaseModel):
    items: List[DanmukuExchangeHistoryItem]
