"""audit_event 写入助手。调用方负责事务边界（与业务变更同事务）。

所有 Decimal 以字符串写入 JSON（不丢精度）；datetime 以 ISO 字符串写入。
快照从已变动的 ORM 对象读取，因此必须在业务值写完之后调用。
"""
from __future__ import annotations
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditEvent, AUDIT_EVENT_TYPES
from app.models.base import Position, Transaction, User


def _j(v: Any) -> Any:
    """JSON 安全化：Decimal→str（保精度），datetime→iso，list/dict 递归。"""
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _j(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_j(x) for x in v]
    return v


def user_snapshot(u: User) -> dict[str, Any]:
    return _j({
        "cash": u.cash,
        "debt": u.debt,
        "debt_last_accrued_at": u.debt_last_accrued_at,
    })


def position_snapshot(p: Optional[Position]) -> dict[str, Any]:
    """p=None 或已删除 → amount 0。"""
    if p is None:
        return {"amount": "0", "cost_basis": "0"}
    return _j({"amount": p.amount, "cost_basis": p.cost_basis})


def market_snapshot(
    *,
    outcome_ids: Iterable[int],
    q: Iterable[Decimal],
    b: float,
    prices: Iterable[float],
    status: str,
) -> dict[str, Any]:
    return {
        "outcome_ids": [int(x) for x in outcome_ids],
        "q": [format(Decimal(str(x)), "f") for x in q],
        "b": float(b),
        "prices": [float(p) for p in prices],
        "status": str(getattr(status, "value", status)),
    }


def record(
    session: AsyncSession,
    event_type: str,
    *,
    user_id: Optional[int] = None,
    market_id: Optional[int] = None,
    outcome_id: Optional[int] = None,
    operator_user_id: Optional[int] = None,
    ref_table: Optional[str] = None,
    ref_id: Optional[int] = None,
    payload: Optional[dict[str, Any]] = None,
    user_after: Optional[dict[str, Any]] = None,
    position_after: Optional[dict[str, Any]] = None,
    market_after: Optional[dict[str, Any]] = None,
    ts: Optional[datetime] = None,
) -> AuditEvent:
    if event_type not in AUDIT_EVENT_TYPES:
        raise ValueError(f"unknown audit event_type: {event_type}")
    ev = AuditEvent(
        event_type=event_type,
        user_id=user_id,
        market_id=market_id,
        outcome_id=outcome_id,
        operator_user_id=operator_user_id,
        ref_table=ref_table,
        ref_id=ref_id,
        payload=_j(payload or {}),
        user_after=user_after,
        position_after=position_after,
        market_after=market_after,
    )
    if ts is not None:
        ev.ts = ts
    session.add(ev)
    return ev


_TX_EVENT = {
    "buy": "trade_buy",
    "sell": "trade_sell",
    "liquidate": "trade_liquidate",
    "settle": "settle_win",
    "settle_lose": "settle_lose",
}


async def record_trade(
    session: AsyncSession,
    *,
    tx: "Transaction",
    user: User,
    position: Optional[Position],
    market_id: int,
    market_after: Optional[dict[str, Any]],
    extra: Optional[dict[str, Any]] = None,
    operator_user_id: Optional[int] = None,
    flush: bool = True,
) -> AuditEvent:
    """Transaction 写入后调用：flush 拿 tx.id，再追加对应审计事件。

    position=None 表示该仓位已删除 / 不存在（position_after.amount=0）。
    market_after 对 buy/sell/liquidate 必填（全市场 q 向量）；settle 传 None。
    flush=False：调用方已自行 flush 过（批量场景，如结算循环——逐条 flush 是
    O(N²) 的 identity map 扫描，审计 P4）；此时 tx.id 必须已赋值。
    """
    if flush:
        await session.flush()
    assert tx.id is not None, "record_trade(flush=False) requires a flushed tx"
    tx_type = str(getattr(tx.type, "value", tx.type))
    payload: dict[str, Any] = {
        "tx_type": tx_type,
        "shares": tx.shares,
        "cost": tx.cost,
        "gross": tx.gross,
        "fee": tx.fee,
        "price": tx.price,
        "pre_market_price": tx.pre_market_price,
        "post_market_price": tx.post_market_price,
    }
    if extra:
        payload.update(extra)
    return record(
        session, _TX_EVENT[tx_type],
        user_id=user.id,
        market_id=market_id,
        outcome_id=tx.outcome_id,
        operator_user_id=operator_user_id,
        ref_table="transaction",
        ref_id=tx.id,
        payload=payload,
        user_after=user_snapshot(user),
        position_after=position_snapshot(position),
        market_after=market_after,
        ts=tx.timestamp,
    )


async def market_snapshot_from_db(
    session: AsyncSession, market_id: int, *, status: Optional[str] = None,
) -> dict[str, Any]:
    """非 writer 路径用：从 DB 读 outcome.total_shares / liquidity_b 组市场快照。
    同事务内调用，读到的是本事务已修改的值。"""
    from sqlalchemy import select as _select
    from app.models.base import Market, Outcome
    from app.services.lmsr import calculate_lmsr_with_prices, quantize_cost
    market = await session.get(Market, market_id)
    outs = (await session.execute(
        _select(Outcome).where(Outcome.market_id == market_id).order_by(Outcome.id.asc())
    )).scalars().all()
    q_dec = [quantize_cost(o.total_shares) for o in outs]
    _, prices = calculate_lmsr_with_prices([float(x) for x in q_dec], float(market.liquidity_b))
    return market_snapshot(
        outcome_ids=[int(o.id) for o in outs], q=q_dec, b=float(market.liquidity_b),
        prices=prices, status=status if status is not None else market.status,
    )


def record_liquidation_repay(session: AsyncSession, user: User, repaid: Decimal,
                             debt_before: Decimal, daily_rate: Decimal, trigger_source: str) -> None:
    """强平后的自动还债（decrease_debt_locked 不写 ledger，审计在这里补）。
    debt_before 取 decrease_debt_locked 调用前的值，用于反推隐式结息。"""
    if repaid <= 0:
        return
    interest = (user.debt + repaid - debt_before).quantize(Decimal("0.000001"))
    record(
        session, "liquidation_repay", user_id=user.id,
        payload={"repaid": repaid, "interest_accrued": interest, "daily_rate": daily_rate,
                 "trigger_source": trigger_source},
        user_after=user_snapshot(user),
    )
