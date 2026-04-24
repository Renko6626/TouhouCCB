"""Loan 业务原子操作。调用方负责事务边界。"""
from __future__ import annotations
from decimal import Decimal
from datetime import datetime

from app.models.base import User


_QUANT = Decimal("0.000001")


def accrue_interest(user: User, daily_rate: Decimal, now: datetime) -> None:
    """把从 user.debt_last_accrued_at 到 now 的利息折进 user.debt。
    复利：每次调用作用在当前 debt 上，增量 = debt * rate * elapsed_sec / 86400。
    debt==0 / last_accrued_at is None / elapsed<=0 时是 no-op。
    """
    if user.debt <= 0 or user.debt_last_accrued_at is None:
        return
    elapsed_sec = (now - user.debt_last_accrued_at).total_seconds()
    if elapsed_sec <= 0:
        return
    factor = Decimal(1) + daily_rate * Decimal(elapsed_sec) / Decimal(86400)
    user.debt = (user.debt * factor).quantize(_QUANT)
    user.debt_last_accrued_at = now
