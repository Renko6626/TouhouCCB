"""Loan 业务原子操作。调用方负责事务边界。"""
from __future__ import annotations
from decimal import Decimal
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

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


class LoanServiceError(Exception):
    pass


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _compat_now(user: User) -> datetime:
    """返回与 user.debt_last_accrued_at 时区感知性一致的当前时间。
    SQLite 不保存 tzinfo，读回的 datetime 可能是 naive；
    若 stored 是 naive 则返回 naive UTC，否则返回 aware UTC。
    """
    now = datetime.now(timezone.utc)
    if user.debt_last_accrued_at is not None and user.debt_last_accrued_at.tzinfo is None:
        return now.replace(tzinfo=None)
    return now


async def increase_debt(
    session: AsyncSession,
    user_id: int,
    amount: Decimal,
    *,
    grant_cash: bool,
    daily_rate: Decimal,
) -> User:
    """SELECT FOR UPDATE user → accrue → debt += amount；grant_cash=True 时 cash += amount。
    调用方负责 commit。amount 必须 > 0，否则 ValueError。
    """
    if amount <= 0:
        raise ValueError("amount must be positive")
    stmt = select(User).where(User.id == user_id).with_for_update()
    result = await session.execute(stmt)
    u = result.scalar_one()
    now = _compat_now(u)
    accrue_interest(u, daily_rate, now)
    u.debt = (u.debt + amount).quantize(_QUANT)
    if u.debt_last_accrued_at is None:
        u.debt_last_accrued_at = now
    if grant_cash:
        u.cash = (u.cash + amount).quantize(_QUANT)
    # 防御性兜底：debt/cash 不应出现负值
    if u.debt < 0 or u.cash < 0:
        raise LoanServiceError(f"invariant violated post-increase: debt={u.debt} cash={u.cash}")
    session.add(u)
    return u


async def decrease_debt(
    session: AsyncSession,
    user_id: int,
    amount: Decimal,
    *,
    consume_cash: bool,
    daily_rate: Decimal,
) -> tuple[User, Decimal]:
    """SELECT FOR UPDATE user → accrue → effective 取「真实负债（含利息）」与（如扣现金）「真实现金」三方最小值 → 扣减。

    edge case 关键：accrue_interest 后的真实 debt 可能因复利大于调用方传入的快照
    估算；直接拿快照预检会让现金被扣到负值。所以这里 effective = min(amount, post-accrual debt[, cash])。
    consume_cash=True 时 cash -= effective；False 时 cash 不变。
    debt 归零时清 last_accrued_at。调用方负责 commit。
    返回 (user, effective_amount)。amount 必须 > 0。
    """
    if amount <= 0:
        raise ValueError("amount must be positive")
    stmt = select(User).where(User.id == user_id).with_for_update()
    result = await session.execute(stmt)
    u = result.scalar_one()
    now = _compat_now(u)
    accrue_interest(u, daily_rate, now)
    effective = min(amount, u.debt).quantize(_QUANT)
    if consume_cash:
        # 杜绝复利场景下「pre-accrual 快照通过预检 + post-accrual 实际超 cash」导致 cash 跑负
        effective = min(effective, u.cash).quantize(_QUANT)
    if effective <= 0:
        return u, Decimal("0")
    u.debt = (u.debt - effective).quantize(_QUANT)
    if consume_cash:
        u.cash = (u.cash - effective).quantize(_QUANT)
    if u.debt <= 0:
        u.debt = Decimal("0")
        u.debt_last_accrued_at = None
    # 防御性兜底：debt/cash 不应出现负值
    if u.debt < 0 or u.cash < 0:
        raise LoanServiceError(f"invariant violated post-decrease: debt={u.debt} cash={u.cash}")
    session.add(u)
    return u, effective


def compute_max_borrow(user: User, holdings_value: Decimal, k: Decimal) -> Decimal:
    """max(0, k × (cash - debt + holdings_value) - debt)"""
    net_worth = user.cash - user.debt + holdings_value
    headroom = k * net_worth - user.debt
    return max(Decimal("0"), headroom).quantize(_QUANT)
