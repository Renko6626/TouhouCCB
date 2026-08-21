"""CloseCmd/ResumeCmd/ResolveCmd op 单元测试（fixture 拷贝自 test_writer_buy.py，文件间不共享 import）。"""
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlmodel import select

from app.core.database import async_session_maker
from app.models.base import (
    Market, MarketStatus, Outcome, Position, Transaction, TransactionType, User,
)
from app.services.market_writer import WRITER
from app.services.writer_ops import BuyCmd, CloseCmd, ResolveCmd, ResumeCmd


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    yield
    await WRITER.stop()


async def _seed_market(status=MarketStatus.TRADING, shares=("3.5", "0")) -> tuple[int, list[int]]:
    async with async_session_maker() as s:
        m = Market(title="t", description="", liquidity_b=100.0, status=status, tags="")
        s.add(m)
        await s.flush()
        oids = []
        for v in shares:
            o = Outcome(market_id=m.id, label=f"o{v}", total_shares=Decimal(v))
            s.add(o)
            await s.flush()
            oids.append(o.id)
        await s.commit()
        return m.id, oids


async def _seed_user(cash="1000") -> int:
    async with async_session_maker() as s:
        u = User(username="alice", casdoor_id="cas_alice", cash=Decimal(cash))
        s.add(u)
        await s.flush()
        uid = u.id
        await s.commit()
        return uid


async def _seed_user2(cash="1000") -> int:
    async with async_session_maker() as s:
        u = User(username="bob", casdoor_id="cas_bob", cash=Decimal(cash))
        s.add(u)
        await s.flush()
        uid = u.id
        await s.commit()
        return uid


def _buy(mid, oid, uid, shares="10", **kw):
    return BuyCmd(market_id=mid, outcome_id=oid, user_id=uid, username="alice",
                  shares=Decimal(shares), max_cost=kw.get("max_cost"),
                  max_slippage_bps=kw.get("max_slippage_bps"),
                  accept_any_slippage=kw.get("accept_any_slippage", False))


@pytest.mark.asyncio
async def test_close_then_resume_updates_db_memory_and_rejects_trades():
    mid, oids = await _seed_market()
    uid = await _seed_user()
    await WRITER.start()
    await WRITER.submit(CloseCmd(market_id=mid))
    assert WRITER.get_state(mid).status == MarketStatus.HALT
    async with async_session_maker() as s:
        assert (await s.get(Market, mid)).status == MarketStatus.HALT
    with pytest.raises(HTTPException):
        await WRITER.submit(_buy(mid, oids[0], uid, accept_any_slippage=True))
    await WRITER.submit(ResumeCmd(market_id=mid))
    assert WRITER.get_state(mid).status == MarketStatus.TRADING
    res = await WRITER.submit(_buy(mid, oids[0], uid, shares="1", accept_any_slippage=True))
    assert res["shares"] == 1.0


@pytest.mark.asyncio
async def test_resume_requires_halt():
    mid, _ = await _seed_market()
    await WRITER.start()
    with pytest.raises(HTTPException) as ei:
        await WRITER.submit(ResumeCmd(market_id=mid))
    assert "不在熔断状态" in ei.value.detail


@pytest.mark.asyncio
async def test_resolve_pays_winner_deletes_positions_and_settles():
    mid, oids = await _seed_market(shares=("0", "0"))
    uid_w = await _seed_user()          # 买赢家
    uid_l = await _seed_user2()         # 买输家
    await WRITER.start()
    await WRITER.submit(_buy(mid, oids[0], uid_w, shares="10", accept_any_slippage=True))
    await WRITER.submit(_buy(mid, oids[1], uid_l, shares="10", accept_any_slippage=True))
    async with async_session_maker() as s:
        cash_w_before = (await s.get(User, uid_w)).cash
    res = await WRITER.submit(ResolveCmd(
        market_id=mid, winning_outcome_id=oids[0], payout=Decimal("1"), admin_id=uid_w))
    assert res.status == MarketStatus.SETTLED
    assert res.winning_outcome_id == oids[0]
    assert res.total_payout == Decimal("10.000000")
    assert res.settled_positions == 2
    st = WRITER.get_state(mid)
    assert st.status == MarketStatus.SETTLED       # 状态留内存，后续交易被拒
    async with async_session_maker() as s:
        assert (await s.get(User, uid_w)).cash == cash_w_before + Decimal("10")
        assert (await s.execute(select(Position))).scalars().first() is None
        lose_tx = (await s.execute(select(Transaction).where(
            Transaction.type == TransactionType.SETTLE_LOSE))).scalars().all()
        assert len(lose_tx) == 1 and lose_tx[0].user_id == uid_l
    with pytest.raises(HTTPException):
        await WRITER.submit(_buy(mid, oids[0], uid_w, accept_any_slippage=True))


@pytest.mark.asyncio
async def test_resolve_idempotent_second_call():
    mid, oids = await _seed_market()
    uid = await _seed_user()
    await WRITER.start()
    await WRITER.submit(ResolveCmd(market_id=mid, winning_outcome_id=oids[0],
                                   payout=Decimal("1"), admin_id=uid))
    res2 = await WRITER.submit(ResolveCmd(market_id=mid, winning_outcome_id=oids[0],
                                          payout=Decimal("1"), admin_id=uid))
    assert res2.total_payout == Decimal("0")       # 与老路径幂等语义一致
    assert res2.settled_positions == 0
