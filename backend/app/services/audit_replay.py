"""audit_event 折叠 / 校验（docs/superpowers/specs/2026-08-22-audit-events-design.md）。

两种用法：
- fold(events)：按 id 序折叠出「T 时刻」的 user / position / market 状态（取每个实体最后一条 *_after）。
- check_consistency(events)：独立重放——用每条事件的 **输入**（shares/cost/delta/interest）从上一状态推
  出本事件应得的状态，与事件自带的 *_after 快照比对。不一致 = 写路径有 bug 或事件漏记。

这里只依赖事件本身，不依赖 LMSR 代码，因此能校验「线上算出来的结果」而非重算一遍同一份代码。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Optional

from app.models.audit import AuditEvent

Q6 = Decimal("0.000001")


def D(v: Any) -> Decimal:
    return Decimal(str(v)).quantize(Q6) if v is not None else Decimal("0")


@dataclass
class UserState:
    cash: Decimal
    debt: Decimal
    anchored: bool          # True = 从 user_register 起就有记录，可做增量校验
    last_event_id: int


@dataclass
class MarketState:
    outcome_ids: list[int]
    q: list[Decimal]
    b: float
    prices: list[float]
    status: str
    anchored: bool
    last_event_id: int


@dataclass
class Snapshot:
    users: dict[int, UserState] = field(default_factory=dict)
    positions: dict[tuple[int, int], Decimal] = field(default_factory=dict)   # (user_id, outcome_id) → amount
    markets: dict[int, MarketState] = field(default_factory=dict)
    last_event_id: int = 0


@dataclass
class Mismatch:
    event_id: int
    event_type: str
    entity: str          # "user:<id>" / "position:<uid>:<oid>" / "market:<id>"
    field: str
    expected: str
    actual: str


# 事件类型 → 对 user 的现金/债务影响（从 payload 推算）
_TRADE_TYPES = {"trade_buy", "trade_sell", "trade_liquidate", "settle_win", "settle_lose"}
_LEDGER_TYPES = {"loan_borrow", "loan_repay", "admin_adjust_cash", "admin_force_loan",
                 "admin_forgive_debt", "admin_amnesty"}


def _expected_user_delta(ev: AuditEvent) -> Optional[tuple[Decimal, Decimal]]:
    """返回 (cash_delta, debt_delta)；None = 该事件不按增量校验（只对齐快照）。"""
    p = ev.payload or {}
    t = ev.event_type
    if t in _TRADE_TYPES:
        # Transaction.cost：buy 为 +支出，sell/liquidate/settle 为 −收入
        return (-D(p.get("cost")), Decimal("0"))
    if t in _LEDGER_TYPES:
        return (D(p.get("cash_delta")), D(p.get("debt_delta")) + D(p.get("interest_accrued")))
    if t == "interest_accrual":
        return (Decimal("0"), D(p.get("interest")))
    if t == "liquidation_repay":
        return (-D(p.get("repaid")), D(p.get("interest_accrued")) - D(p.get("repaid")))
    if t in ("redeem_purchase", "danmuku_exchange"):
        return (-D(p.get("amount")), Decimal("0"))
    if t == "liquidation":
        return None   # 汇总事件：快照必须等于当前状态，但不带增量
    return None


def _expected_position_after(ev: AuditEvent, prev: Decimal) -> Optional[Decimal]:
    p = ev.payload or {}
    t = ev.event_type
    if t == "trade_buy":
        return (prev + D(p.get("shares"))).quantize(Q6)
    if t in ("trade_sell", "trade_liquidate"):
        return (prev - D(p.get("shares"))).quantize(Q6)
    if t in ("settle_win", "settle_lose"):
        return Decimal("0")
    return None


def _expected_q_after(ev: AuditEvent, prev: MarketState) -> Optional[list[Decimal]]:
    p = ev.payload or {}
    t = ev.event_type
    if t in ("trade_buy", "trade_sell", "trade_liquidate"):
        if ev.outcome_id not in prev.outcome_ids:
            return None
        idx = prev.outcome_ids.index(ev.outcome_id)
        q = list(prev.q)
        sh = D(p.get("shares"))
        q[idx] = (q[idx] + sh if t == "trade_buy" else q[idx] - sh).quantize(Q6)
        return q
    if t in ("market_close", "market_resume", "market_settle"):
        return list(prev.q)
    return None


def fold(events: Iterable[AuditEvent], *, check: bool = False) -> tuple[Snapshot, list[Mismatch]]:
    """按 id 升序折叠。check=True 时同时做增量一致性校验。"""
    snap = Snapshot()
    mism: list[Mismatch] = []

    def bad(ev, entity, fld, exp, act):
        mism.append(Mismatch(ev.id, ev.event_type, entity, fld, str(exp), str(act)))

    for ev in events:
        snap.last_event_id = ev.id
        uid = ev.user_id

        # ── user ──
        if uid is not None and ev.user_after is not None:
            after_cash, after_debt = D(ev.user_after.get("cash")), D(ev.user_after.get("debt"))
            prev = snap.users.get(uid)
            if ev.event_type == "user_register":
                snap.users[uid] = UserState(after_cash, after_debt, True, ev.id)
            else:
                if check and prev is not None and prev.anchored:
                    delta = _expected_user_delta(ev)
                    if delta is not None:
                        exp_cash = (prev.cash + delta[0]).quantize(Q6)
                        exp_debt = (prev.debt + delta[1]).quantize(Q6)
                        if exp_cash != after_cash:
                            bad(ev, f"user:{uid}", "cash", exp_cash, after_cash)
                        if exp_debt != after_debt:
                            bad(ev, f"user:{uid}", "debt", exp_debt, after_debt)
                    elif ev.event_type == "liquidation":
                        if prev.cash != after_cash:
                            bad(ev, f"user:{uid}", "cash(summary)", prev.cash, after_cash)
                        if prev.debt != after_debt:
                            bad(ev, f"user:{uid}", "debt(summary)", prev.debt, after_debt)
                snap.users[uid] = UserState(after_cash, after_debt,
                                            prev.anchored if prev else False, ev.id)

        # ── position ──
        if uid is not None and ev.outcome_id is not None and ev.position_after is not None:
            key = (uid, ev.outcome_id)
            after_amt = D(ev.position_after.get("amount"))
            if check:
                prev_amt = snap.positions.get(key)
                user_anchored = snap.users.get(uid) is not None and snap.users[uid].anchored
                if prev_amt is None and user_anchored:
                    prev_amt = Decimal("0")   # 注册后首次出现的仓位，之前必为 0
                if prev_amt is not None:
                    exp = _expected_position_after(ev, prev_amt)
                    if exp is not None and exp != after_amt:
                        bad(ev, f"position:{uid}:{ev.outcome_id}", "amount", exp, after_amt)
            snap.positions[key] = after_amt

        # ── market ──
        if ev.market_id is not None and ev.market_after is not None:
            ma = ev.market_after
            q_after = [D(x) for x in ma.get("q", [])]
            prev_m = snap.markets.get(ev.market_id)
            if ev.event_type == "market_create":
                anchored = True
            else:
                anchored = prev_m.anchored if prev_m else False
                if check and prev_m is not None and prev_m.anchored:
                    exp_q = _expected_q_after(ev, prev_m)
                    if exp_q is not None and exp_q != q_after:
                        bad(ev, f"market:{ev.market_id}", "q", exp_q, q_after)
            snap.markets[ev.market_id] = MarketState(
                outcome_ids=[int(x) for x in ma.get("outcome_ids", [])],
                q=q_after, b=float(ma.get("b", 0)),
                prices=[float(x) for x in ma.get("prices", [])],
                status=str(ma.get("status")), anchored=anchored, last_event_id=ev.id,
            )
        # 结算：该市场所有仓位归零（settle_win/lose 已逐条写 position_after=0，这里兜底）
        if ev.event_type == "market_settle" and ev.market_id is not None:
            m = snap.markets.get(ev.market_id)
            if m:
                for key in list(snap.positions):
                    if key[1] in m.outcome_ids:
                        snap.positions[key] = Decimal("0")

    return snap, mism


async def load_events(session, *, upto_id: Optional[int] = None, upto_ts=None,
                      since_ts=None) -> list[AuditEvent]:
    from sqlalchemy import select
    stmt = select(AuditEvent).order_by(AuditEvent.id.asc())
    if upto_id is not None:
        stmt = stmt.where(AuditEvent.id <= upto_id)
    if upto_ts is not None:
        stmt = stmt.where(AuditEvent.ts <= upto_ts)
    if since_ts is not None:
        stmt = stmt.where(AuditEvent.ts >= since_ts)
    return list((await session.execute(stmt)).scalars().all())


async def compare_with_live(session, snap: Snapshot) -> list[Mismatch]:
    """把折叠结果与线上 user / position / outcome 表比对（只比事件里出现过的实体）。"""
    from sqlalchemy import select
    from app.models.base import Outcome, Position, User
    out: list[Mismatch] = []
    if snap.users:
        rows = (await session.execute(select(User).where(User.id.in_(list(snap.users))))).scalars().all()
        for u in rows:
            st = snap.users[u.id]
            if D(u.cash) != st.cash:
                out.append(Mismatch(st.last_event_id, "live", f"user:{u.id}", "cash", str(st.cash), str(D(u.cash))))
            if D(u.debt) != st.debt:
                out.append(Mismatch(st.last_event_id, "live", f"user:{u.id}", "debt", str(st.debt), str(D(u.debt))))
    if snap.positions:
        uids = sorted({k[0] for k in snap.positions})
        rows = (await session.execute(select(Position).where(Position.user_id.in_(uids)))).scalars().all()
        live = {(p.user_id, p.outcome_id): D(p.amount) for p in rows}
        for key, amt in snap.positions.items():
            if live.get(key, Decimal("0")) != amt:
                out.append(Mismatch(0, "live", f"position:{key[0]}:{key[1]}", "amount", str(amt), str(live.get(key, Decimal("0")))))
    if snap.markets:
        oids = [oid for m in snap.markets.values() for oid in m.outcome_ids]
        rows = (await session.execute(select(Outcome).where(Outcome.id.in_(oids)))).scalars().all()
        live_q = {o.id: D(o.total_shares) for o in rows}
        for mid, m in snap.markets.items():
            for oid, q in zip(m.outcome_ids, m.q):
                if live_q.get(oid) != q:
                    out.append(Mismatch(m.last_event_id, "live", f"market:{mid}", f"q[{oid}]", str(q), str(live_q.get(oid))))
    return out
