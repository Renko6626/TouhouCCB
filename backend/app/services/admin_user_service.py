"""管理员对用户「资金 / 贷款 / 账号」的操作 — 业务层。

路由层（api/v1/admin_users.py）只做鉴权、参数校验、调用本模块、写审计日志。
本模块的函数：
- 自己管事务边界（`managed_transaction`），返回纯 dict，便于单测与复用
- 业务错误抛 `AdminUserError(status, detail)`，路由层翻译成 HTTPException
- 所有资金变动都写 LedgerEntry（同事务）
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import managed_transaction
from app.models.base import User
from app.services import ledger_service, loan_service, site_config
from app.services import audit_service

_CENT = Decimal("0.01")

# 批量操作安全围栏：一次最多影响这么多用户，超过 → 400（防 filter 不严误伤）
BATCH_HARDCAP = 500


class AdminUserError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _money(v: Decimal) -> float:
    return float(v.quantize(_CENT))


async def _lock_user(db: AsyncSession, user_id: int) -> User:
    u = (await db.execute(
        select(User).where(User.id == user_id).with_for_update()
    )).scalar_one_or_none()
    if u is None:
        raise AdminUserError(404, "用户不存在")
    return u


# ==========================================
# 单用户操作
# ==========================================

async def adjust_cash(
    db: AsyncSession, *, target_id: int, amount: Decimal, reason: str, admin_id: int,
) -> Dict[str, Any]:
    async with managed_transaction(db):
        u = await _lock_user(db, target_id)
        new_cash = u.cash + amount
        if new_cash < 0:
            raise AdminUserError(400, f"操作后现金为 {new_cash}，不能为负")
        u.cash = new_cash
        ledger_service.record_entry(
            db, user=u, entry_type="admin_adjust_cash",
            cash_delta=amount, debt_delta=Decimal("0"), daily_rate=None,
            operator_user_id=admin_id, reason=reason,
        )
    return {
        "user_id": u.id, "username": u.username,
        "amount": float(amount), "new_cash": _money(new_cash), "reason": reason,
    }


async def force_loan(
    db: AsyncSession, *, target_id: int, amount: Decimal, reason: str, admin_id: int,
) -> Dict[str, Any]:
    if not await site_config.get_bool(db, "loan_enabled"):
        raise AdminUserError(403, "借款功能已关闭")
    rate = await site_config.get_decimal(db, "loan_daily_rate")
    if await db.get(User, target_id) is None:
        raise AdminUserError(404, "用户不存在")
    try:
        u = await loan_service.increase_debt(
            db, target_id, amount, grant_cash=True, daily_rate=rate,
            source="admin_force_loan", operator_user_id=admin_id, reason=reason,
        )
    except (ValueError, loan_service.LoanServiceError) as e:
        await db.rollback()
        raise AdminUserError(400, str(e))
    await db.commit()
    await db.refresh(u)
    return {"user_id": u.id, "cash": _money(u.cash), "debt": _money(u.debt)}


async def forgive_debt(
    db: AsyncSession, *, target_id: int, amount: Decimal, reason: str, admin_id: int,
) -> Dict[str, Any]:
    rate = await site_config.get_decimal(db, "loan_daily_rate")
    if await db.get(User, target_id) is None:
        raise AdminUserError(404, "用户不存在")
    try:
        u, effective = await loan_service.decrease_debt(
            db, target_id, amount, consume_cash=False, daily_rate=rate,
            source="admin_forgive_debt", operator_user_id=admin_id, reason=reason,
        )
    except (ValueError, loan_service.LoanServiceError) as e:
        await db.rollback()
        raise AdminUserError(400, str(e))
    await db.commit()
    await db.refresh(u)
    return {
        "user_id": u.id, "cash": _money(u.cash), "debt": _money(u.debt),
        "effective": _money(effective),
    }


async def set_role(
    db: AsyncSession, *, target_id: int, is_admin: bool, admin_id: int,
) -> Dict[str, Any]:
    """安全围栏：不能改自己；不能取消最后一个管理员。"""
    if target_id == admin_id:
        raise AdminUserError(400, "不能修改自己的管理员权限")
    async with managed_transaction(db):
        t = await _lock_user(db, target_id)
        if t.is_superuser == is_admin:
            return {"user_id": t.id, "username": t.username, "is_admin": t.is_superuser, "changed": False}
        if not is_admin:
            n = int((await db.execute(
                select(func.count()).select_from(User).where(User.is_superuser == True)  # noqa: E712
            )).scalar_one())
            if n <= 1:
                raise AdminUserError(400, "不能取消最后一个管理员")
        t.is_superuser = is_admin
        audit_service.record(
            db, "admin_set_role", user_id=t.id, operator_user_id=admin_id,
            payload={"is_superuser_before": not is_admin, "is_superuser_after": is_admin},
        )
    return {"user_id": t.id, "username": t.username, "is_admin": t.is_superuser, "changed": True}


async def ban(db: AsyncSession, *, target_id: int, admin_id: int) -> Dict[str, Any]:
    """复用 is_active（FastAPI Users 标准）：被封用户访问任何 protected endpoint 自动 401。
    安全围栏：不能封自己；不能封最后一个活跃管理员。"""
    if target_id == admin_id:
        raise AdminUserError(400, "不能封禁自己")
    async with managed_transaction(db):
        t = await _lock_user(db, target_id)
        if t.is_superuser and t.is_active:
            n = int((await db.execute(
                select(func.count()).select_from(User).where(
                    User.is_superuser == True, User.is_active == True,  # noqa: E712
                )
            )).scalar_one())
            if n <= 1:
                raise AdminUserError(400, "不能封禁最后一个活跃管理员（先取消管理员权限或先提升另一人）")
        was_active = t.is_active
        t.is_active = False
        if was_active:
            audit_service.record(db, "admin_ban", user_id=t.id, operator_user_id=admin_id)
    return {"user_id": t.id, "username": t.username, "is_active": False, "changed": was_active}


async def unban(db: AsyncSession, *, target_id: int, admin_id: Optional[int] = None) -> Dict[str, Any]:
    async with managed_transaction(db):
        t = await _lock_user(db, target_id)
        was_active = t.is_active
        t.is_active = True
        if not was_active:
            audit_service.record(db, "admin_unban", user_id=t.id, operator_user_id=admin_id)
    return {"user_id": t.id, "username": t.username, "is_active": True, "changed": not was_active}


# ==========================================
# 批量操作
# ==========================================

class UserFilter(BaseModel):
    user_id_min: Optional[int] = Field(default=None, description="包含")
    user_id_max: Optional[int] = Field(default=None, description="包含")
    cash_min: Optional[Decimal] = None
    cash_max: Optional[Decimal] = None
    debt_min: Optional[Decimal] = None
    debt_max: Optional[Decimal] = None
    is_active: Optional[bool] = Field(default=None, description="None=不过滤")
    include_superuser: bool = Field(default=False, description="默认排除超管，避免误改自己")


def build_user_filter_stmt(f: UserFilter):
    """select(User) + filter 条件，按 id ASC（事务内锁顺序一致防死锁）。"""
    stmt = select(User)
    if f.user_id_min is not None:
        stmt = stmt.where(User.id >= f.user_id_min)
    if f.user_id_max is not None:
        stmt = stmt.where(User.id <= f.user_id_max)
    if f.cash_min is not None:
        stmt = stmt.where(User.cash >= f.cash_min)
    if f.cash_max is not None:
        stmt = stmt.where(User.cash <= f.cash_max)
    if f.debt_min is not None:
        stmt = stmt.where(User.debt >= f.debt_min)
    if f.debt_max is not None:
        stmt = stmt.where(User.debt <= f.debt_max)
    if f.is_active is not None:
        stmt = stmt.where(User.is_active == f.is_active)
    if not f.include_superuser:
        stmt = stmt.where(User.is_superuser == False)  # noqa: E712
    return stmt.order_by(User.id.asc())


async def _preview_users(db: AsyncSession, f: UserFilter) -> List[User]:
    users = (await db.execute(build_user_filter_stmt(f))).scalars().all()
    if len(users) > BATCH_HARDCAP:
        raise AdminUserError(400, f"匹配用户数 {len(users)} 超过单批上限 {BATCH_HARDCAP}，请收紧 filter")
    return users


async def _lock_users(db: AsyncSession, f: UserFilter) -> List[User]:
    users = (await db.execute(build_user_filter_stmt(f).with_for_update())).scalars().all()
    if len(users) > BATCH_HARDCAP:
        raise AdminUserError(400, f"加锁后匹配 {len(users)} > 上限，操作中止")
    return users


async def batch_adjust_cash(
    db: AsyncSession, *, f: UserFilter, amount: Decimal, reason: str, admin_id: int, dry_run: bool,
) -> Dict[str, Any]:
    """围栏：amount≠0；hardcap；操作后 cash<0 的用户跳过（记 failed）；单事务 FOR UPDATE。"""
    if amount == 0:
        raise AdminUserError(400, "amount 不能为 0")

    preview = await _preview_users(db, f)
    matched = [
        {
            "id": u.id, "username": u.username,
            "cash_before": _money(u.cash), "debt": _money(u.debt),
            "cash_after": _money(u.cash + amount),
            "will_fail": (u.cash + amount) < 0,
        }
        for u in preview
    ]
    will_fail = sum(1 for m in matched if m["will_fail"])
    eligible = len(matched) - will_fail
    if dry_run:
        return {
            "dry_run": True,
            "matched_count": len(matched), "eligible_count": eligible, "will_fail_count": will_fail,
            "total_delta": _money(Decimal(eligible) * amount),
            "matched_users": matched,
        }

    updated: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    async with managed_transaction(db):
        for u in await _lock_users(db, f):
            new_cash = u.cash + amount
            if new_cash < 0:
                failed.append({
                    "user_id": u.id, "username": u.username,
                    "reason": "操作后现金为负，已跳过",
                    "cash_before": _money(u.cash), "would_be": _money(new_cash),
                })
                continue
            before = u.cash
            u.cash = new_cash
            ledger_service.record_entry(
                db, user=u, entry_type="admin_adjust_cash",
                cash_delta=amount, debt_delta=Decimal("0"), daily_rate=None,
                operator_user_id=admin_id, reason=reason,
            )
            updated.append({
                "user_id": u.id, "username": u.username,
                "cash_before": _money(before), "cash_after": _money(new_cash),
            })
    return {
        "dry_run": False,
        "updated_count": len(updated), "failed_count": len(failed),
        "total_delta": _money(Decimal(len(updated)) * amount),
        "updated": updated, "failed": failed,
    }


async def amnesty(
    db: AsyncSession, *, f: UserFilter, reset_cash_to: Optional[Decimal],
    forgive_debt: bool, reason: str, admin_id: int, dry_run: bool,
) -> Dict[str, Any]:
    """「大赦天下」：匹配用户债务清零（先结息）+ 现金设为目标值。持仓不动。

    每人写一条 LedgerEntry(admin_amnesty, cash_delta=目标-当前, debt_delta=-结息后债务)。
    cash 高于目标的用户同样被降到目标值（这是"还原"不是"补足"）。
    """
    if reset_cash_to is None:
        reset_cash_to = await site_config.get_decimal(db, "initial_balance")
    if reset_cash_to < 0:
        raise AdminUserError(400, "reset_cash_to 不能为负")
    rate = await site_config.get_decimal(db, "loan_daily_rate")

    preview = await _preview_users(db, f)
    rows = [
        {
            "id": u.id, "username": u.username,
            "cash_before": _money(u.cash), "cash_after": _money(reset_cash_to),
            "debt_before": _money(u.debt), "debt_after": 0.0 if forgive_debt else _money(u.debt),
        }
        for u in preview
    ]
    if dry_run:
        return {
            "dry_run": True,
            "matched_count": len(rows),
            "reset_cash_to": _money(reset_cash_to),
            "forgive_debt": forgive_debt,
            "total_cash_delta": _money(sum((reset_cash_to - u.cash for u in preview), Decimal("0"))),
            # 预览按未结息 debt 估算；实际执行会先结息，略大于此值
            "total_debt_forgiven": _money(sum((u.debt for u in preview), Decimal("0"))) if forgive_debt else 0.0,
            "matched_users": rows,
        }

    updated: List[Dict[str, Any]] = []
    total_cash_delta = Decimal("0")
    total_forgiven = Decimal("0")
    async with managed_transaction(db):
        for u in await _lock_users(db, f):
            cash_before, debt_before = u.cash, u.debt
            forgiven = Decimal("0")
            if forgive_debt and u.debt > 0:
                # 先显式结息，再按结息后的全额清零（decrease_debt_locked 内部再次
                # accrue 是同一时刻的 no-op）
                loan_service.accrue_interest(u, rate, loan_service._compat_now(u))
                forgiven = await loan_service.decrease_debt_locked(
                    db, u, u.debt, consume_cash=False, daily_rate=rate,
                )
            cash_delta = (reset_cash_to - u.cash).quantize(Decimal("0.000001"))
            u.cash = reset_cash_to
            ledger_service.record_entry(
                db, user=u, entry_type="admin_amnesty",
                cash_delta=cash_delta, debt_delta=-forgiven,
                daily_rate=rate if forgiven > 0 else None,
                operator_user_id=admin_id, reason=reason,
            )
            total_cash_delta += cash_delta
            total_forgiven += forgiven
            updated.append({
                "user_id": u.id, "username": u.username,
                "cash_before": _money(cash_before), "cash_after": _money(u.cash),
                "debt_before": _money(debt_before), "debt_after": _money(u.debt),
                "debt_forgiven": _money(forgiven),
            })
    return {
        "dry_run": False,
        "updated_count": len(updated),
        "reset_cash_to": _money(reset_cash_to),
        "forgive_debt": forgive_debt,
        "total_cash_delta": _money(total_cash_delta),
        "total_debt_forgiven": _money(total_forgiven),
        "updated": updated,
    }
