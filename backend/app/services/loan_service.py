"""Loan 业务原子操作。调用方负责事务边界。"""
from __future__ import annotations
from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.models.base import User
from app.services import audit_service, ledger_service


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
    source: str,
    operator_user_id: Optional[int] = None,
    reason: Optional[str] = None,
) -> User:
    """SELECT FOR UPDATE user → accrue → debt += amount；grant_cash=True 时 cash += amount。
    调用方负责 commit。amount 必须 > 0，否则 ValueError。

    source: ledger entry_type（"borrow" / "admin_force_loan"）。
    """
    if amount <= 0:
        raise ValueError("amount must be positive")
    stmt = select(User).where(User.id == user_id).with_for_update()
    result = await session.execute(stmt)
    u = result.scalar_one()
    now = _compat_now(u)
    debt_pre_accrual = u.debt
    accrue_interest(u, daily_rate, now)
    interest = (u.debt - debt_pre_accrual).quantize(_QUANT)
    u.debt = (u.debt + amount).quantize(_QUANT)
    if u.debt_last_accrued_at is None:
        u.debt_last_accrued_at = now
    if grant_cash:
        u.cash = (u.cash + amount).quantize(_QUANT)
    # 防御性兜底：debt/cash 不应出现负值
    if u.debt < 0 or u.cash < 0:
        raise LoanServiceError(f"invariant violated post-increase: debt={u.debt} cash={u.cash}")
    await ledger_service.record_entry(
        session, user=u, entry_type=source,
        cash_delta=(amount if grant_cash else Decimal("0")),
        debt_delta=amount,
        daily_rate=daily_rate,
        operator_user_id=operator_user_id,
        reason=reason,
        interest_accrued=interest,
    )
    session.add(u)
    return u


async def decrease_debt_locked(
    session: AsyncSession,
    user: User,
    amount: Decimal,
    *,
    consume_cash: bool,
    daily_rate: Decimal,
) -> Decimal:
    """对已 lock 的 user 对象做 decrease_debt 核心算法，**不 SELECT FOR UPDATE**。

    调用方负责：
    - user 已被 lock_user / SELECT FOR UPDATE 拿到
    - 在同一事务内 commit

    省 1 个 SELECT FOR UPDATE round trip + 1 次 flush (vs 老的 decrease_debt
    要先 flush 把 user.cash 落库才能让内部 SELECT 读到)。直接改 in-memory
    user 对象的 cash/debt，调用方拿到的 user 已是最新值。

    edge case 同 decrease_debt：accrue_interest 后的真实 debt 可能因复利大于
    调用方快照，effective 取 min(amount, debt[, cash])。

    返回 effective 金额。amount 必须 > 0。
    """
    if amount <= 0:
        raise ValueError("amount must be positive")
    now = _compat_now(user)
    accrue_interest(user, daily_rate, now)
    effective = min(amount, user.debt).quantize(_QUANT)
    if consume_cash:
        # 杜绝复利场景下「pre-accrual 快照通过预检 + post-accrual 实际超 cash」导致 cash 跑负
        effective = min(effective, user.cash).quantize(_QUANT)
    if effective <= 0:
        return Decimal("0")
    user.debt = (user.debt - effective).quantize(_QUANT)
    if consume_cash:
        user.cash = (user.cash - effective).quantize(_QUANT)
    if user.debt <= 0:
        user.debt = Decimal("0")
        user.debt_last_accrued_at = None
    # 防御性兜底：debt/cash 不应出现负值
    if user.debt < 0 or user.cash < 0:
        raise LoanServiceError(f"invariant violated post-decrease: debt={user.debt} cash={user.cash}")
    session.add(user)
    return effective


async def decrease_debt(
    session: AsyncSession,
    user_id: int,
    amount: Decimal,
    *,
    consume_cash: bool,
    daily_rate: Decimal,
    source: str,
    operator_user_id: Optional[int] = None,
    reason: Optional[str] = None,
) -> tuple[User, Decimal]:
    """SELECT FOR UPDATE user → accrue → effective 扣减 → 写 ledger。

    原 API，调用方传 user_id，内部 lock。需要避免 lock 的 hot path 用
    decrease_debt_locked。

    source: ledger entry_type（"repay" / "admin_forgive_debt"）。
    返回 (user, effective_amount)。
    """
    if amount <= 0:
        raise ValueError("amount must be positive")
    stmt = select(User).where(User.id == user_id).with_for_update()
    result = await session.execute(stmt)
    u = result.scalar_one()
    debt_before = u.debt
    before_at = u.debt_last_accrued_at
    effective = await decrease_debt_locked(
        session, u, amount,
        consume_cash=consume_cash, daily_rate=daily_rate,
    )
    # debt_after = debt_before + interest − effective → 反推隐式结息，精确到 6dp
    interest = (u.debt + effective - debt_before).quantize(_QUANT)
    if effective > 0:
        await ledger_service.record_entry(
            session, user=u, entry_type=source,
            cash_delta=(-effective if consume_cash else Decimal("0")),
            debt_delta=-effective,
            daily_rate=daily_rate,
            operator_user_id=operator_user_id,
            reason=reason,
            interest_accrued=interest,
        )
    elif interest != 0:
        # 本次没还上（effective 量化为 0）但结息已改写 u.debt 并会随调用方 commit——
        # 不能让这笔利息变动没有事件，否则折叠器从此对不上
        audit_service.record(
            session, "interest_accrual", user_id=u.id,
            payload={"debt_before": debt_before, "debt_after": u.debt, "interest": interest,
                     "daily_rate": daily_rate, "source": f"{source}_noop",
                     "elapsed_sec": (u.debt_last_accrued_at - before_at).total_seconds()
                     if before_at and u.debt_last_accrued_at else None},
            user_after=audit_service.user_snapshot(u),
        )
    return u, effective


def compute_max_borrow(user: User, holdings_value: Decimal, k: Decimal) -> Decimal:
    """max(0, k × (cash - debt + holdings_value) - debt)"""
    net_worth = user.cash - user.debt + holdings_value
    headroom = k * net_worth - user.debt
    return max(Decimal("0"), headroom).quantize(_QUANT)
