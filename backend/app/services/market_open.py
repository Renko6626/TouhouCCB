"""「市场可交易」统一判定。

TRADING 但已过 closes_at 的市场：用户不能买卖（与 _require_trading 一致），
因此其持仓也**不能**算作可变现抵押（LCV）、**不应**被强平卖出、且与 HALT 一样给予强平豁免。
四处口径（买卖门槛 / LCV / 强平 / 豁免）都走这里，避免再出现不对称。
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional

from app.models.base import MarketStatus


def market_is_open(status, closes_at: Optional[datetime], now: Optional[datetime] = None) -> bool:
    if status != MarketStatus.TRADING:
        return False
    if closes_at is None:
        return True
    now = now or datetime.now(timezone.utc)
    if closes_at.tzinfo is None:          # SQLite 读回 naive
        closes_at = closes_at.replace(tzinfo=timezone.utc)
    return now < closes_at
