"""强平 run / action 持久化（计划 §3.2 冻结签名；spec §5 F3）。

幂等与并发要点：

- 每用户最多一个 ``status='active'`` 的 run（部分唯一索引
  ``uq_liquidation_run_active_user`` 兜底）；``get_or_create_active_run`` 先锁
  **User 行**再 ``SELECT ... FOR UPDATE`` active run，把并发创建串行化到与执行
  路径相同的锁序（User → run/action），不依赖 SAVEPOINT。
  （不用 ``begin_nested()`` 兜唯一键：SQLAlchemy 在建立 savepoint 前会 flush，
  冲突会打废外层事务；pysqlite 下 RELEASE 最外层 savepoint 还会提交外层事务。）
- ``record_action`` 以 ``(run_id, round_no)`` 唯一键做幂等：先锁 run 行再看是否
  已有动作，重复调用返回**已存在的动作**，不重复累计 run 合计、不重复卖仓/还债
  （崩溃后重扫安全）。唯一约束仍是数据库层最后防线。
- **锁后必须刷新**（reviewer blocker 2）：``expire_on_commit=False`` 下调用方持有的
  run 可能是旧快照；``_lock_run_row`` 用 ``FOR UPDATE + populate_existing`` 强制读
  数据库当前行，totals/rounds/终态一律以刷新后的行为准，旧对象无法覆盖已提交值。
- ``find_action_for_round`` 是给 WP7 的**效果前**幂等查询：不可逆动作（卖仓/还债）
  之前先看该轮是否已提交，避免崩溃重扫重复执行。
- 不 commit：事务边界一律由调用方负责；``run.rounds`` / ``next_round`` 只在
  调用方提交时对外可见（"round 只在提交时前进"）。
- ``close_run`` 只允许 active → 终态；同终态重复调用幂等，其它终态转换拒绝。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy import inspect as sa_inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import User
from app.models.credit import (
    ACTION_KINDS,
    FEE_CURRENCIES,
    RUN_STATUSES,
    LiquidationAction,
    LiquidationRun,
)
from app.services.credit.keys import PRODUCTS

ZERO = Decimal("0")


class RunStateError(RuntimeError):
    """run 状态机/参数非法（调用方 bug 或崩溃恢复路径异常）。"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _run_id_of(run: LiquidationRun) -> int:
    """不触发 lazy load 地取 run 主键。

    session rollback 会把对象置为 expired；直接读 ``run.id`` 会在 async 上下文里
    触发同步 IO（MissingGreenlet）。instance identity 在过期后仍保留，因此用它。
    """
    identity = sa_inspect(run).identity
    if identity is not None:
        return int(identity[0])
    raw = run.__dict__.get("id")
    if raw is None:
        raise RunStateError("run 尚未 flush（id 为空）")
    return int(raw)


def _as_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, Decimal):
        parsed = value
    else:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"{name} 不是合法 Decimal: {value!r}") from exc
    if not parsed.is_finite():
        raise ValueError(f"{name} 必须是有限 Decimal: {value!r}")
    return parsed


async def _lock_run_row(session: AsyncSession, run_id: int) -> LiquidationRun:
    """``SELECT ... FOR UPDATE`` + ``populate_existing``：锁住并**刷新** ORM 实例。

    reviewer blocker 2：``expire_on_commit=False`` 下调用方手里的 run 可能是旧快照；
    只锁 id 再读旧对象会把已提交的 totals/rounds/终态覆盖掉。这里强制用数据库
    当前行刷新 identity map（调用方同一 session 的实例会被就地刷新）。
    """
    stmt = (
        select(LiquidationRun)
        .where(LiquidationRun.id == int(run_id))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    row = (await session.execute(stmt)).scalars().first()
    if row is None:
        raise RunStateError(f"run {run_id} 不存在")
    return row


async def get_or_create_active_run(
    session: AsyncSession,
    *,
    user_id: int,
    trigger_source: str,
    now: datetime,
) -> LiquidationRun:
    """返回该用户当前的 active run；没有则在当前事务内创建（不 commit）。

    先锁 User 行再锁 active run：与执行路径锁序一致，并发创建被串行化，
    不依赖 SAVEPOINT（见模块 docstring）。命中已有 run 时用 ``populate_existing``
    刷新 identity map，避免把旧 totals/rounds 带给调用方。
    """
    uid = int(user_id)
    if not str(trigger_source):
        raise ValueError("trigger_source 不能为空")
    if len(str(trigger_source)) > 32:
        raise ValueError(f"trigger_source 超过 32 字符: {trigger_source!r}")

    # 锁序：User → run/action。第二个创建者会在这里等待，拿到后再查已存在的 run。
    await session.execute(
        select(User.id).where(User.id == uid).with_for_update()
    )
    stmt = (
        select(LiquidationRun)
        .where(LiquidationRun.user_id == uid, LiquidationRun.status == "active")
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    existing = (await session.execute(stmt)).scalars().first()
    if existing is not None:
        return existing

    run = LiquidationRun(
        user_id=uid,
        status="active",
        trigger_source=str(trigger_source),
        started_at=now,
        updated_at=now,
    )
    session.add(run)
    await session.flush()
    return run


async def find_action_for_round(
    session: AsyncSession,
    *,
    run: LiquidationRun,
    round_no: int,
) -> Optional[LiquidationAction]:
    """效果发生**之前**的幂等查询（WP7 接口）：该 ``(run, round)`` 是否已有动作。

    只读、不加锁。WP7 在卖仓/还债等不可逆效果前调用它跳过已提交轮次；
    ``record_action`` 在效果之后会再查一次并以唯一键兜底。
    """
    run_id = _run_id_of(run)
    if round_no < 1:
        raise ValueError(f"round_no 必须 >= 1: {round_no!r}")
    return (
        await session.execute(
            select(LiquidationAction).where(
                LiquidationAction.run_id == run_id,
                LiquidationAction.round_no == round_no,
            )
        )
    ).scalars().first()


async def record_action(
    session: AsyncSession,
    *,
    run: LiquidationRun,
    round_no: int,
    kind: str,
    product: Optional[str] = None,
    group_id: Optional[int] = None,
    mode: Optional[str] = None,
    requested: Optional[dict] = None,
    executed: Optional[dict] = None,
    proceeds: Decimal = ZERO,
    fee: Decimal = ZERO,
    fee_currency: Optional[str] = None,
    repaid: Decimal = ZERO,
    debt_after: Decimal = ZERO,
    cash_after: Decimal = ZERO,
    economic_version_after: int = 0,
    blocked_reason: Optional[str] = None,
) -> LiquidationAction:
    """记录本轮动作；``(run_id, round_no)`` 已存在时幂等返回既有动作。"""
    run_id = _run_id_of(run)
    if kind not in ACTION_KINDS:
        raise ValueError(f"未知 action kind: {kind!r}（允许 {ACTION_KINDS}）")
    if round_no < 1:
        raise ValueError(f"round_no 必须 >= 1: {round_no!r}")
    if fee_currency is not None and fee_currency not in FEE_CURRENCIES:
        raise ValueError(f"未知 fee_currency: {fee_currency!r}（允许 {FEE_CURRENCIES}）")
    if product is not None and product not in PRODUCTS:
        raise ValueError(f"未知 product: {product!r}（允许 {PRODUCTS}）")
    if mode is not None and mode not in ("partial", "full"):
        raise ValueError(f"未知 mode: {mode!r}（允许 partial/full）")
    if blocked_reason is not None and len(str(blocked_reason)) > 255:
        raise ValueError("blocked_reason 超过 255 字符")

    # 锁 run 行并**刷新**实例：串行化并发重放，且 totals/rounds 用数据库当前值
    # 累计（reviewer blocker 2：expire_on_commit=False 下旧对象会覆盖已提交值）。
    locked = await _lock_run_row(session, run_id)
    if locked.status != "active":
        raise RunStateError(f"run {locked.id} 状态 {locked.status!r} 不是 active，拒绝记录动作")
    existing = (
        await session.execute(
            select(LiquidationAction).where(
                LiquidationAction.run_id == locked.id,
                LiquidationAction.round_no == round_no,
            )
        )
    ).scalars().first()
    if existing is not None:
        return existing

    action = LiquidationAction(
        run_id=locked.id,
        user_id=locked.user_id,
        round_no=round_no,
        kind=kind,
        product=product,
        group_id=group_id if group_id is None else int(group_id),
        mode=mode,
        requested=requested,
        executed=executed,
        proceeds=_as_decimal(proceeds, "proceeds"),
        fee=_as_decimal(fee, "fee"),
        fee_currency=fee_currency,
        repaid=_as_decimal(repaid, "repaid"),
        debt_after=_as_decimal(debt_after, "debt_after"),
        cash_after=_as_decimal(cash_after, "cash_after"),
        economic_version_after=int(economic_version_after),
        blocked_reason=blocked_reason,
        created_at=_utcnow(),
    )
    session.add(action)
    await session.flush()

    # 只在动作确实新增时前进 round / 累计合计（与 action 同事务，由调用方 commit）
    locked.rounds = max(int(locked.rounds or 0), round_no)
    if round_no >= int(locked.next_round or 1):
        locked.next_round = round_no + 1
    locked.total_proceeds = _as_decimal(locked.total_proceeds, "total_proceeds") + action.proceeds
    locked.total_repaid = _as_decimal(locked.total_repaid, "total_repaid") + action.repaid
    locked.total_fee = _as_decimal(locked.total_fee, "total_fee") + action.fee
    if kind == "sell_group":
        locked.last_group_product = product
        locked.last_group_id = None if group_id is None else int(group_id)
    if blocked_reason is not None:
        locked.last_blocked_reason = str(blocked_reason)
    locked.updated_at = _utcnow()
    return action


async def close_run(
    session: AsyncSession,
    *,
    run: LiquidationRun,
    status: str,
    now: datetime,
) -> None:
    """把 active run 关闭为终态；同终态重复调用幂等。

    锁行 + ``populate_existing`` 刷新（reviewer blocker 2）：调用方持有的旧快照
    （status='active'）不能覆盖数据库里已被并发关闭的终态。
    """
    if status not in RUN_STATUSES:
        raise ValueError(f"未知 run status: {status!r}（允许 {RUN_STATUSES}）")
    if status == "active":
        raise RunStateError("close_run 不能把 run 关闭为 active")
    run_id = _run_id_of(run)
    locked = await _lock_run_row(session, run_id)
    if locked.status == status:
        return
    if locked.status != "active":
        raise RunStateError(
            f"run {locked.id} 已是终态 {locked.status!r}，不能再关闭为 {status!r}"
        )
    locked.status = status
    locked.closed_at = now
    locked.updated_at = now
    await session.flush()
