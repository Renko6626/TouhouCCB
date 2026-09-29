"""Loan 业务原子操作。调用方负责事务边界。"""
from __future__ import annotations
from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.models.base import User
from app.services import audit_service, ledger_service
from app.services.credit.version import bump_economic_version


_QUANT = Decimal("0.000001")


def _require_writes():
    # Recheck after row-lock waits: ownership may have been lost while queued.
    from app.services.credit import flags
    from app.services.credit.ownership import OWNERSHIP
    config = flags.get_flags()
    if (config.unified_credit_enabled or config.read_only_instance
            or flags.read_only_from_env() or OWNERSHIP.reason is not None):
        OWNERSHIP.require_writes()


def interest_factor(daily_rate: Decimal, elapsed_sec: Decimal | float | int) -> Decimal:
    """按「日利率 r、按日复利」口径：factor = (1 + r) ^ (elapsed / 86400)。

    这个形式对分段调用**精确可合成**：(1+r)^a · (1+r)^b = (1+r)^(a+b)，
    所以不管 sweep 每 10s 还是每小时跑一次，一天后的结果都恰好是 debt × (1+r)。
    （旧版用 1 + r·Δt/day 逐 tick 相乘，等价于连续复利 e^r，10%/日时实际 10.5%。）
    """
    return (Decimal(1) + daily_rate) ** (Decimal(str(elapsed_sec)) / Decimal(86400))


def accrue_interest(user: User, daily_rate: Decimal, now: datetime) -> None:
    """把从 user.debt_last_accrued_at 到 now 的利息折进 user.debt（见 interest_factor）。
    debt==0 / last_accrued_at is None / elapsed<=0 时是 no-op。

    增量量化到 6dp 后为 0 时**不推进** debt_last_accrued_at：否则小额债务（6dp 下
    一个 sweep 间隔的利息不足 0.0000005）永远累不出利息；不推进则时间继续累积，
    到够一个 LSB 时才结，长期利息不丢。

    读写同源：数值完全来自 ``pending_debt``，估值侧（统一风控 E 的 D_effective）
    必须调用同一个函数，禁止另写一份利息公式。
    """
    new_debt = pending_debt(user, daily_rate, now)
    if new_debt == user.debt:
        return
    user.debt = new_debt
    user.debt_last_accrued_at = now


def _as_utc(dt: datetime) -> datetime:
    """naive 时间按 UTC 解释（SQLite 读回的 DATETIME 没有 tzinfo）。"""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _elapsed_seconds(stored: datetime, now: datetime) -> float:
    # 两边 tz 感知性一致时保持原样相减（与历史行为逐位一致）；混用时按 UTC 对齐，
    # 避免估值侧传入 aware now、库里是 naive 时直接 TypeError。
    if (stored.tzinfo is None) != (now.tzinfo is None):
        return (_as_utc(now) - _as_utc(stored)).total_seconds()
    return (now - stored).total_seconds()


def pending_debt(user: User, daily_rate: Decimal, now: datetime) -> Decimal:
    """**纯函数**：返回含未落库利息的债务（D_effective），不修改 user。

    debt==0 / last_accrued_at is None / elapsed<=0 时返回当前 user.debt。
    结果量化到 6dp —— 与 ``accrue_interest`` 完全同源，所以"先估值再加息"与
    "先加息再估值"得到同一个 D（不会出现估值读到的 D 比落库后的 D 少一分钱）。
    """
    if user.debt <= 0 or user.debt_last_accrued_at is None:
        return user.debt
    elapsed_sec = _elapsed_seconds(user.debt_last_accrued_at, now)
    if elapsed_sec <= 0:
        return user.debt
    return (user.debt * interest_factor(daily_rate, elapsed_sec)).quantize(_QUANT)


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
    now: Optional[datetime] = None,
) -> User:
    """SELECT FOR UPDATE user → accrue → debt += amount；grant_cash=True 时 cash += amount。
    调用方负责 commit。amount 必须 > 0，否则 ValueError。

    source: ledger entry_type（"borrow" / "admin_force_loan"）。
    """
    if amount <= 0:
        raise ValueError("amount must be positive")
    stmt = select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    result = await session.execute(stmt)
    u = result.scalar_one()
    _require_writes()
    now = now or _compat_now(u)
    debt_pre_accrual = u.debt
    accrue_interest(u, daily_rate, now)
    interest = (u.debt - debt_pre_accrual).quantize(_QUANT)
    # A new principal starts accruing at this operation's timestamp even when
    # the old principal's pending interest rounded to zero. Keeping its old
    # timestamp would retroactively charge the new loan historical interest.
    u.debt_last_accrued_at = now
    u.debt = (u.debt + amount).quantize(_QUANT)
    if grant_cash:
        u.cash = (u.cash + amount).quantize(_QUANT)
    # 防御性兜底：debt/cash 不应出现负值
    if u.debt < 0 or u.cash < 0:
        raise LoanServiceError(f"invariant violated post-increase: debt={u.debt} cash={u.cash}")
    bump_economic_version(u)
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
    amount: Decimal | None,
    *,
    consume_cash: bool,
    daily_rate: Decimal,
    now: Optional[datetime] = None,
) -> Decimal:
    """对已 lock 的 user 对象做 decrease_debt 核心算法，**不 SELECT FOR UPDATE**。

    now：调用方若已自行 accrue_interest 过，必须把同一个 now 传进来——否则两次
    结息相差的毫秒在大额债务下会产生 6dp 非零增量，留下 ~1e-5「灰尘债」且
    ledger debt_delta 与快照不等（审计 M2）。

    调用方负责：
    - user 已被 lock_user / SELECT FOR UPDATE 拿到
    - 在同一事务内 commit

    省 1 个 SELECT FOR UPDATE round trip + 1 次 flush (vs 老的 decrease_debt
    要先 flush 把 user.cash 落库才能让内部 SELECT 读到)。直接改 in-memory
    user 对象的 cash/debt，调用方拿到的 user 已是最新值。

    edge case 同 decrease_debt：accrue_interest 后的真实 debt 可能因复利大于
    调用方快照，effective 取 min(amount, debt[, cash])。

    返回 effective 金额。amount 必须 > 0；None 表示按结息后的最新债务还到上限。
    """
    if amount is not None and amount <= 0:
        raise ValueError("amount must be positive")
    if now is None:
        now = _compat_now(user)
    _require_writes()
    before = (user.cash, user.debt, user.debt_last_accrued_at)
    accrue_interest(user, daily_rate, now)
    effective = (user.debt if amount is None else min(amount, user.debt)).quantize(_QUANT)
    if consume_cash:
        # 杜绝复利场景下「pre-accrual 快照通过预检 + post-accrual 实际超 cash」导致 cash 跑负
        effective = min(effective, user.cash).quantize(_QUANT)
    if effective <= 0:
        if before != (user.cash, user.debt, user.debt_last_accrued_at):
            bump_economic_version(user)
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
    bump_economic_version(user)
    session.add(user)
    return effective


async def decrease_debt(
    session: AsyncSession,
    user_id: int,
    amount: Decimal | None,
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
    返回 (user, effective_amount)。amount=None 在锁内结息后按最新 debt/cash 还到上限。
    """
    if amount is not None and amount <= 0:
        raise ValueError("amount must be positive")
    stmt = select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
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
