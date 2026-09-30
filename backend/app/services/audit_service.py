"""audit_event 写入助手。调用方负责事务边界（与业务变更同事务）。

所有 Decimal 以字符串写入 JSON（不丢精度）；datetime 以 ISO 字符串写入。
快照从已变动的 ORM 对象读取，因此必须在业务值写完之后调用。
"""
from __future__ import annotations
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditEvent, AUDIT_EVENT_TYPES
from app.models.base import Position, Transaction, User


def _j(v: Any) -> Any:
    """JSON 安全化：Decimal→str（保精度），datetime/date→iso，list/dict 递归。

    `datetime` 是 `date` 的子类，必须先判断 `datetime`，再把纯 `date`
    （例如 FX treasury.spend_date）转成 ISO 字符串，否则 JSON 列无法序列化。
    """
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _j(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_j(x) for x in v]
    return v


def _utc_iso(dt: Optional[datetime]) -> Optional[str]:
    """Serialize a timestamp as an unambiguous UTC ISO-8601 string.

    SQLite does not persist ``tzinfo``, so a value written as UTC reads back
    naive.  Replay must not have to guess the zone; treat naive input as UTC
    and always emit an explicit offset instead of a floating local string.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


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


def record_fx_short_interest(
    session: AsyncSession,
    *,
    user: User,
    position: Any,
    pair_id: int,
    interest: Decimal,
    daily_rate: Decimal,
    elapsed_sec: Optional[float],
    interest_last_accrued_at_before: Optional[datetime],
    accrued_at: datetime,
    source: str,
) -> AuditEvent:
    """Append the audit package for accrued foreign (short) interest.

    Foreign interest only moves ``FxShortPosition.interest_foreign`` and its
    clock, never ``User.cash`` or ``User.debt``.  The event keeps the generic
    ``interest_accrual`` type so existing replay anchors stay valid; the event's
    ``interest`` field therefore carries the gold delta (zero here) and the
    foreign increment is explicit under ``currency='foreign'``.  WP5 replay
    folds the ``interest_foreign_*`` leg into the short position.

    ``accrued_at`` is the exact UTC T handed to ``accrue_short_interest``.
    ``interest_last_accrued_at_after`` is read from the committed row, so a
    nonzero event records the clock that actually governs later compounding
    (``accrue_short_interest`` leaves that clock untouched when the quantized
    increment is zero, and the caller then emits no event).  ``AuditEvent.ts``
    is the same T, not a second ``datetime.now()`` sample.
    """
    return record(
        session, "interest_accrual", user_id=user.id,
        ref_table="fx_short_position", ref_id=position.id,
        payload={
            "currency": "foreign",
            "pair_id": pair_id,
            "short_position_id": position.id,
            "interest": Decimal("0"),
            "interest_foreign_delta": interest,
            "principal_foreign": position.principal_foreign,
            "interest_foreign_before": position.interest_foreign - interest,
            "interest_foreign_after": position.interest_foreign,
            "interest_last_accrued_at_before": _utc_iso(interest_last_accrued_at_before),
            "interest_last_accrued_at_after": _utc_iso(position.interest_last_accrued_at),
            "accrued_at": _utc_iso(accrued_at),
            "daily_rate": daily_rate,
            "elapsed_sec": elapsed_sec,
            "source": source,
        },
        user_after=user_snapshot(user),
        ts=accrued_at,
    )


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


def record_fx_trade(session: AsyncSession, *, trade: Any, user: Optional[User], pair: Any,
                    wallet: Any, treasury: Any) -> AuditEvent:
    """Record the replay payload for a committed FX pool trade."""
    payload = {
            "pair_id": pair.id, "side": trade.side,
            "input_amount": trade.input_amount, "output_amount": trade.output_amount,
            "fee_amount": trade.fee_amount,
            "pre_gold_reserve": trade.pre_gold_reserve,
            "pre_foreign_reserve": trade.pre_foreign_reserve,
            "post_gold_reserve": trade.post_gold_reserve,
            "post_foreign_reserve": trade.post_foreign_reserve,
            "post_price": trade.post_price,
            "pool_after": {"gold": pair.gold_reserve, "foreign": pair.foreign_reserve},
            "pool_version": pair.pool_version,
            "source": trade.source,
            "wallet_after": None if wallet is None else {
                "foreign_amount": wallet.foreign_amount,
                "cost_basis": wallet.cost_basis,
                "updated_at": wallet.updated_at,
            },
            "treasury_after": {
                "gold_balance": treasury.gold_balance,
                "foreign_balance": treasury.foreign_balance,
                "daily_spend": treasury.daily_spend,
                "spend_date": treasury.spend_date,
                "updated_at": treasury.updated_at,
            },
        }
    return record(
        session, "fx_trade", user_id=None if user is None else user.id,
        ref_table="fx_trade", ref_id=trade.id,
        payload=payload, user_after=None if user is None else user_snapshot(user),
    )


def record_fx_short_open(
    session: AsyncSession,
    *,
    trade: Any,
    user: User,
    pair: Any,
    position: Any,
    treasury: Any,
    treasury_before: dict[str, Any],
    user_before: dict[str, Any],
    short_before: dict[str, Any],
    pool_before: dict[str, Any],
    accrued_at: datetime,
) -> AuditEvent:
    """Purpose-aware replay package for one opening/add-to-short borrow-sell.

    Keeps the generic ``fx_trade`` event so existing user/pool/treasury replay
    anchors stay valid, but carries the short-specific legs WP5 needs: the
    borrowed quantity, the gold proceeds that moved into ``User.cash`` and the
    short lock/basis, the treasury before/after (it really pays Q out and takes
    the fee back), the operation identity (``idempotency_key``), the user
    economic version and the short row before/after.  ``wallet_after`` is
    explicitly ``None``: opening never credits ``FxWallet``, so replay must not
    expect a spot wallet delta.
    """
    payload = {
        "accrued_at": _utc_iso(accrued_at),
        "pair_id": pair.id,
        "purpose": trade.purpose,
        "side": trade.side,
        "input_amount": trade.input_amount,
        "output_amount": trade.output_amount,
        "fee_amount": trade.fee_amount,
        "requested_foreign_amount": trade.requested_foreign_amount,
        "min_gold_out": trade.min_out,
        "borrowed_foreign": trade.input_amount,
        "gold_proceeds": trade.output_amount,
        "pre_gold_reserve": trade.pre_gold_reserve,
        "pre_foreign_reserve": trade.pre_foreign_reserve,
        "post_gold_reserve": trade.post_gold_reserve,
        "post_foreign_reserve": trade.post_foreign_reserve,
        "post_price": trade.post_price,
        "pool_before": pool_before,
        "pool_after": {"gold": pair.gold_reserve, "foreign": pair.foreign_reserve},
        "pool_version": pair.pool_version,
        "source": trade.source,
        "idempotency_key": trade.idempotency_key,
        "user_economic_version": user.economic_version,
        "wallet_after": None,
        "treasury_before": treasury_before,
        # ``gold``/``foreign`` mirror the fund/withdraw snapshot shape; the
        # ``*_balance`` spelling is what the generic ``fx_trade`` fold anchors
        # on (same as ``record_fx_trade``), so carry both from the locked row.
        "treasury_after": {
            "gold": treasury.gold_balance,
            "foreign": treasury.foreign_balance,
            "gold_balance": treasury.gold_balance,
            "foreign_balance": treasury.foreign_balance,
            "daily_spend": treasury.daily_spend,
            "spend_date": treasury.spend_date,
            "updated_at": treasury.updated_at,
        },
        "user_before": user_before,
        "user_after": {
            "cash": user.cash,
            "debt": user.debt,
            "debt_last_accrued_at": user.debt_last_accrued_at,
        },
        "short_before": short_before,
        "short_after": {
            "short_position_id": position.id,
            "principal_foreign": position.principal_foreign,
            "interest_foreign": position.interest_foreign,
            "interest_last_accrued_at": position.interest_last_accrued_at,
            "restricted_gold": position.restricted_gold,
            "proceeds_basis_gold": position.proceeds_basis_gold,
        },
    }
    return record(
        session, "fx_trade", user_id=user.id,
        ref_table="fx_trade", ref_id=trade.id,
        payload=payload, user_after=user_snapshot(user),
        ts=accrued_at,
    )


def record_fx_short_cover(
    session: AsyncSession,
    *,
    trade: Any,
    user: User,
    pair: Any,
    position: Any,
    treasury: Any,
    treasury_before: dict[str, Any],
    user_before: dict[str, Any],
    short_before: dict[str, Any],
    pool_before: dict[str, Any],
    interest_paid_foreign: Decimal,
    principal_paid_foreign: Decimal,
    released_lock: Decimal,
    allocated_proceeds_basis: Decimal,
    realized_pl: Decimal,
    accrued_at: datetime,
    full_cover: bool,
    limited_by_cash: bool = False,
) -> AuditEvent:
    """Purpose-aware replay package for one exact-output short cover.

    Keeps the generic ``fx_trade`` event so the user/pool replay anchors stay
    valid, with the cover-specific legs: real gold paid (``input_amount``) and
    the buy fee in gold, the exact foreign repaid and its interest/principal
    split, the released lock (which may exceed the proportional base on a losing
    cover), the independently apportioned historical proceeds basis, the
    realized P/L (allocated basis minus real gold), the treasury before/after it
    actually received q, the user cash/debt before/after, the short row before/
    after, the operation key and the user economic version.  ``wallet_after`` is
    explicitly ``None``: covering never moves the spot wallet.  ``accrued_at`` is
    the common UTC T used for every interest leg.  ``limited_by_cash`` is True
    only for a forced cover whose budget capped the output below the planned
    quantity; a player cover always passes False.  WP5 folds these legs into the
    purpose-aware replay.
    """
    payload = {
        "pair_id": pair.id,
        "purpose": trade.purpose,
        "side": trade.side,
        "input_amount": trade.input_amount,
        "output_amount": trade.output_amount,
        "fee_amount": trade.fee_amount,
        "fee_currency": "gold",
        "requested_foreign_amount": trade.requested_foreign_amount,
        "cover_all": trade.cover_all,
        "max_gold_in": trade.max_gold_in,
        "paid_gold": trade.input_amount,
        "repaid_foreign": trade.output_amount,
        "interest_paid_foreign": interest_paid_foreign,
        "principal_paid_foreign": principal_paid_foreign,
        "released_lock": released_lock,
        "allocated_proceeds_basis": allocated_proceeds_basis,
        "realized_pl": realized_pl,
        "full_cover": full_cover,
        "limited_by_cash": bool(limited_by_cash),
        "pre_gold_reserve": trade.pre_gold_reserve,
        "pre_foreign_reserve": trade.pre_foreign_reserve,
        "post_gold_reserve": trade.post_gold_reserve,
        "post_foreign_reserve": trade.post_foreign_reserve,
        "post_price": trade.post_price,
        "accrued_at": _utc_iso(accrued_at),
        "pool_before": pool_before,
        "pool_after": {"gold": pair.gold_reserve, "foreign": pair.foreign_reserve},
        "pool_version": pair.pool_version,
        "source": trade.source,
        "idempotency_key": trade.idempotency_key,
        "user_economic_version": user.economic_version,
        "wallet_after": None,
        "treasury_before": treasury_before,
        # ``gold``/``foreign`` mirror the fund/withdraw snapshot shape and the
        # ``*_balance`` spelling is what the generic fold anchors on.
        "treasury_after": {
            "gold": treasury.gold_balance,
            "foreign": treasury.foreign_balance,
            "gold_balance": treasury.gold_balance,
            "foreign_balance": treasury.foreign_balance,
            "daily_spend": treasury.daily_spend,
            "spend_date": treasury.spend_date,
            "updated_at": treasury.updated_at,
        },
        "user_before": user_before,
        "user_after": {
            "cash": user.cash,
            "debt": user.debt,
            "debt_last_accrued_at": user.debt_last_accrued_at,
        },
        "short_before": short_before,
        "short_after": {
            "short_position_id": position.id,
            "principal_foreign": position.principal_foreign,
            "interest_foreign": position.interest_foreign,
            "interest_last_accrued_at": position.interest_last_accrued_at,
            "restricted_gold": position.restricted_gold,
            "proceeds_basis_gold": position.proceeds_basis_gold,
        },
    }
    return record(
        session, "fx_trade", user_id=user.id,
        ref_table="fx_trade", ref_id=trade.id,
        payload=payload, user_after=user_snapshot(user),
        ts=accrued_at,
    )
