"""LMSR 组强平费率（普通卖出费率、不额外罚金）。

覆盖：
- 非零费率：`cash` 增量恰为 `gross - fee`（fee 只从 gross 扣一次）、逐腿 fee 量化、
  组 fee = 逐腿 fee 之和。
- F12：partial 向上取整到 1 股并封顶持仓；小数持仓被 ceil 一波清掉。
- 负收益腿跳过不删（不卖、不改 q），组内其余腿继续。

运行：
    DATABASE_URL=sqlite+aiosqlite:////dev/shm/credit-wp5.db \
        python -m pytest -q tests/test_lmsr_liquidation_fee.py
"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select, update as sa_update

from app.core.database import async_session_maker
from app.models.base import (
    Market, MarketStatus, Outcome, Position, Transaction, TransactionType, User,
)
from app.services.credit.lmsr_quote import BLOCKED_NEGATIVE_PROCEEDS
from app.services.credit.runs import get_or_create_active_run
from app.services.lmsr import quantize_cost
from app.services.market_writer import WRITER
from app.services.writer_ops import LiquidateGroupCmd

ZERO = Decimal("0")
FEE_RATE = Decimal("0.02")


@pytest_asyncio.fixture(autouse=True)
async def _stop_writer():
    yield
    await WRITER.stop()


async def _seed_market(shares=("0", "0"), *, status=MarketStatus.TRADING):
    async with async_session_maker() as s:
        async with s.begin():
            m = Market(title="wp5fee", description="", liquidity_b=100.0,
                       status=status, tags="")
            s.add(m)
            await s.flush()
            oids = []
            for i, value in enumerate(shares):
                o = Outcome(market_id=m.id, label=f"o{i}", total_shares=Decimal(value))
                s.add(o)
                await s.flush()
                oids.append(int(o.id))
            return int(m.id), oids


async def _seed_user(*, cash="0", debt="50", username="wp5fee"):
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=username, casdoor_id=f"cas_{username}",
                     cash=Decimal(cash), debt=Decimal(debt), is_active=True,
                     debt_last_accrued_at=None)
            s.add(u)
            await s.flush()
            return int(u.id)


async def _give_position(uid: int, oid: int, amount: str, cost: str) -> None:
    """直接布景：写 Position 并把份额同步进 outcome 镜像（与真实买入后的世界一致）。"""
    async with async_session_maker() as s:
        async with s.begin():
            s.add(Position(user_id=uid, outcome_id=oid,
                           amount=Decimal(amount), cost_basis=Decimal(cost)))
            await s.flush()
            await s.execute(
                sa_update(Outcome).where(Outcome.id == oid)
                .values(total_shares=Outcome.total_shares + Decimal(amount)))


async def _new_run(uid: int) -> int:
    async with async_session_maker() as s:
        async with s.begin():
            run = await get_or_create_active_run(
                s, user_id=uid, trigger_source="test",
                now=datetime.now(timezone.utc))
            return int(run.id)


def _group_cmd(mid, uid, run_id, *, mode, partial_pct, fee_rate, round_no=1):
    return LiquidateGroupCmd(
        market_id=mid, user_id=uid, run_id=run_id, round_no=round_no, mode=mode,
        partial_pct=partial_pct, fee_rate=fee_rate, daily_rate=ZERO,
        trigger_source="test")


async def _user_money(uid: int) -> tuple[Decimal, Decimal]:
    async with async_session_maker() as s:
        u = await s.get(User, uid)
        return u.cash, u.debt


async def _positions(uid: int, relative: dict[int, int]):
    async with async_session_maker() as s:
        rows = (await s.execute(
            select(Position).where(Position.user_id == uid).order_by(Position.outcome_id)
        )).scalars().all()
        return [(relative[int(p.outcome_id)], p.amount, p.cost_basis) for p in rows]


async def _outcome_q(oids: list[int]) -> list[Decimal]:
    async with async_session_maker() as s:
        rows = (await s.execute(
            select(Outcome).where(Outcome.id.in_(oids)).order_by(Outcome.id)
        )).scalars().all()
        return [o.total_shares for o in rows]


async def _liq_rows(uid: int, relative: dict[int, int]):
    async with async_session_maker() as s:
        rows = (await s.execute(
            select(Transaction)
            .where(Transaction.user_id == uid,
                   Transaction.type == TransactionType.LIQUIDATE)
            .order_by(Transaction.id)
        )).scalars().all()
        return [
            (relative[int(t.outcome_id)], t.shares, t.cost, t.gross, t.fee, t.price,
             t.pre_market_price, t.post_market_price, tuple(t.market_prices_post))
            for t in rows
        ]










@pytest.mark.asyncio
async def test_fee_charged_once_cash_increment_equals_net():
    """F5：fee = gross × 费率（逐腿量化），cash 增量 == net == gross − fee。"""
    mid, oids = await _seed_market(("0", "0"))
    uid = await _seed_user(cash="0", debt="0", username="fee_user")
    await _give_position(uid, oids[0], "20", "10")
    await _give_position(uid, oids[1], "8", "3")
    await WRITER.start()
    run_id = await _new_run(uid)

    res = await WRITER.submit(_group_cmd(
        mid, uid, run_id, mode="full", partial_pct=Decimal("1"), fee_rate=FEE_RATE))

    assert res["sold_count"] == 2
    assert res["gross"] > ZERO
    assert res["fee"] > ZERO
    assert res["net"] == res["gross"] - res["fee"]

    cash, debt = await _user_money(uid)
    assert cash == res["net"], "cash 增量必须恰为 net；出现 gross-2*fee 说明费率被扣了两次"
    assert debt == ZERO

    rel = {oid: i for i, oid in enumerate(oids)}
    rows = await _liq_rows(uid, rel)
    assert len(rows) == 2
    assert sum(row[4] for row in rows) == res["fee"], "组 fee 必须等于逐腿量化 fee 之和"
    async with async_session_maker() as s:
        txs = (await s.execute(
            select(Transaction)
            .where(Transaction.user_id == uid,
                   Transaction.type == TransactionType.LIQUIDATE)
            .order_by(Transaction.id)
        )).scalars().all()
    for tx in txs:
        assert tx.fee == (tx.gross * FEE_RATE).quantize(Decimal("0.000001"))
        assert tx.cost == -(tx.gross - tx.fee)
        assert tx.market_prices_post is not None


@pytest.mark.asyncio
async def test_partial_mode_ceils_to_one_share_and_caps_at_position():
    """F12：partial 向上取整到 1 股、封顶持仓；<1 股零碎持仓被一波清掉。"""
    mid, oids = await _seed_market(("0", "0", "0"))
    uid = await _seed_user(cash="0", debt="0", username="partial_user")
    await _give_position(uid, oids[0], "25", "10")
    await _give_position(uid, oids[1], "0.5", "0.2")
    await _give_position(uid, oids[2], "1", "0.4")
    await WRITER.start()
    run_id = await _new_run(uid)

    res = await WRITER.submit(_group_cmd(
        mid, uid, run_id, mode="partial", partial_pct=Decimal("0.10"), fee_rate=ZERO))

    assert res["sold_count"] == 3
    rel = {oid: i for i, oid in enumerate(oids)}
    sold = {idx: shares for idx, shares, *_ in await _liq_rows(uid, rel)}
    assert sold[0] == Decimal("3.000000")      # ceil(25 × 10%) = 3
    assert sold[1] == Decimal("0.500000")      # ceil(0.05) = 1 ≥ 0.5 → 封顶全卖
    assert sold[2] == Decimal("1.000000")      # ceil(0.1) = 1 == 持仓 → 全卖
    assert [(idx, amount) for idx, amount, _ in await _positions(uid, rel)] == [
        (0, Decimal("22.000000")),
    ]


@pytest.mark.asyncio
async def test_full_mode_sells_everything_and_mirrors_q():
    mid, oids = await _seed_market(("0", "0"))
    uid = await _seed_user(cash="0", debt="0", username="full_user")
    await _give_position(uid, oids[0], "20", "10")
    await _give_position(uid, oids[1], "6", "2")
    await WRITER.start()
    run_id = await _new_run(uid)

    res = await WRITER.submit(_group_cmd(
        mid, uid, run_id, mode="full", partial_pct=Decimal("1"), fee_rate=ZERO))

    assert res["sold_count"] == 2
    rel = {oid: i for i, oid in enumerate(oids)}
    assert await _positions(uid, rel) == []
    assert await _outcome_q(oids) == [ZERO, ZERO]
    assert WRITER.get_state(mid).q_dec == [ZERO, ZERO]


@pytest.mark.asyncio
async def test_negative_proceeds_leg_skipped_not_deleted(monkeypatch):
    """负收益腿跳过（不卖、不删持仓、不改 q），组内其余腿继续。"""
    from app.services.credit import lmsr_quote

    def _fake_cost(q, b):
        # 卖 outcome#0 时 cost 上升（gross < 0），卖 outcome#1 时 cost 下降（gross > 0）
        return 1000.0 - 10.0 * float(q[0]) + 100.0 * float(q[1])

    monkeypatch.setattr(lmsr_quote, "calculate_lmsr_cost", _fake_cost)

    mid, oids = await _seed_market(("0", "0"))
    uid = await _seed_user(cash="0", debt="0", username="negative_user")
    await _give_position(uid, oids[0], "10", "5")
    await _give_position(uid, oids[1], "10", "5")
    await WRITER.start()
    run_id = await _new_run(uid)

    res = await WRITER.submit(_group_cmd(
        mid, uid, run_id, mode="partial", partial_pct=Decimal("0.5"), fee_rate=ZERO))

    assert res["sold_count"] == 1
    rel = {oid: i for i, oid in enumerate(oids)}
    rows = await _liq_rows(uid, rel)
    assert [row[0] for row in rows] == [1]
    assert rows[0][1] == Decimal("5.000000")
    # 被跳过的腿：持仓与镜像 q 都不动
    assert await _positions(uid, rel) == [
        (0, Decimal("10.000000"), Decimal("5.000000")),
        (1, Decimal("5.000000"), Decimal("2.500000")),
    ]
    assert await _outcome_q(oids) == [Decimal("10.000000"), Decimal("5.000000")]


@pytest.mark.asyncio
async def test_all_legs_negative_is_blocked_with_reason(monkeypatch):
    """全腿负收益 → 整组阻塞（blocked_reason），零写入。"""
    from app.services.credit import lmsr_quote

    monkeypatch.setattr(
        lmsr_quote, "calculate_lmsr_cost",
        lambda q, b: 1000.0 - 10.0 * float(q[0]) - 10.0 * float(q[1]))

    mid, oids = await _seed_market(("0", "0"))
    uid = await _seed_user(cash="0", debt="0", username="all_negative")
    await _give_position(uid, oids[0], "10", "5")
    await WRITER.start()
    run_id = await _new_run(uid)

    res = await WRITER.submit(_group_cmd(
        mid, uid, run_id, mode="full", partial_pct=Decimal("1"), fee_rate=ZERO))

    assert res["sold_count"] == 0
    assert res["blocked_reason"] == BLOCKED_NEGATIVE_PROCEEDS
    assert res["net"] == ZERO
    rel = {oid: i for i, oid in enumerate(oids)}
    assert await _positions(uid, rel) == [(0, Decimal("10.000000"), Decimal("5.000000"))]
    assert await _outcome_q(oids) == [Decimal("10.000000"), ZERO]
    cash, _ = await _user_money(uid)
    assert cash == ZERO
