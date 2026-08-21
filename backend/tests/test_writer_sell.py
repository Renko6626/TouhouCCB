"""SellCmd op 单元测试。fixture/helper 从 test_writer_buy.py 拷贝，不跨文件 import。"""
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
from app.services.writer_ops import BuyCmd, SellCmd


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    # conftest 的 autouse setup_db 已在每测试前 drop+create 全表并 seed 好
    # site_config；这里不重复 drop/create，只负责本文件专属的 WRITER 收尾。
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


async def _seed_user(cash="1000", username="alice") -> int:
    async with async_session_maker() as s:
        u = User(username=username, casdoor_id=f"cas_{username}", cash=Decimal(cash))
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


def _sell(mid, oid, uid, shares="5", **kw):
    return SellCmd(market_id=mid, outcome_id=oid, user_id=uid, username="alice",
                   shares=Decimal(shares), min_proceeds=kw.get("min_proceeds"),
                   max_slippage_bps=kw.get("max_slippage_bps"),
                   accept_any_slippage=kw.get("accept_any_slippage", False))


@pytest.mark.asyncio
async def test_sell_roundtrip_conserves_cash():
    """买 10 卖 10（fee=0）→ 现金精确回到起点，q 回到 0。"""
    mid, oids = await _seed_market(shares=("0", "0"))
    uid = await _seed_user(cash="1000")
    await WRITER.start()
    await WRITER.submit(_buy(mid, oids[0], uid, shares="10", accept_any_slippage=True))
    await WRITER.submit(_sell(mid, oids[0], uid, shares="10", accept_any_slippage=True))
    st = WRITER.get_state(mid)
    assert st.q_dec[0] == Decimal("0.000000")
    async with async_session_maker() as s:
        u = await s.get(User, uid)
        # LMSR 往返 + 6dp 量化，现金误差 ≤ 2 个 LSB
        assert abs(u.cash - Decimal("1000")) <= Decimal("0.000002")
        o = await s.get(Outcome, oids[0])
        assert o.total_shares == Decimal("0.000000")
        pos = (await s.execute(select(Position).where(Position.user_id == uid))).scalars().first()
        assert pos.amount == Decimal("0.000000")
        assert pos.cost_basis == Decimal("0.000000")   # 清仓归零


@pytest.mark.asyncio
async def test_sell_insufficient_position_rejected_in_db_txn():
    """隔离测「持仓不足」——需要市场总量 ≥ 卖出量，只有该用户自己的持仓不足。

    若只有单一交易者，总量恒等于其持仓，会先撞 Step 1 的内存总量守卫
    （见 test_sell_market_total_insufficient，那是专门测这条内存守卫的用例）。
    这里额外让第二个用户买入，把市场总量撑到卖出量之上，从而单独隔离出
    「持仓不足」这条 DB 事务内检查，不与内存总量守卫混淆。
    """
    mid, oids = await _seed_market(shares=("0", "0"))
    uid = await _seed_user()
    other_uid = await _seed_user(cash="1000", username="bob")
    await WRITER.start()
    await WRITER.submit(_buy(mid, oids[0], uid, shares="5", accept_any_slippage=True))
    await WRITER.submit(_buy(mid, oids[0], other_uid, shares="10", accept_any_slippage=True))
    with pytest.raises(HTTPException) as ei:
        await WRITER.submit(_sell(mid, oids[0], uid, shares="10", accept_any_slippage=True))
    assert ei.value.detail == "持仓不足"
    st = WRITER.get_state(mid)
    assert st.q_dec[0] == Decimal("15.000000")   # 内存未动（两笔 buy 后的总量）


@pytest.mark.asyncio
async def test_sell_fee_applied_from_site_config():
    from sqlalchemy import text
    from app.services.site_config import clear_cache
    mid, oids = await _seed_market(shares=("0", "0"))
    uid = await _seed_user()
    async with async_session_maker() as s:
        await s.execute(text(
            "INSERT INTO siteconfig (key, value, value_type, updated_at) "
            "VALUES ('sell_fee_rate', '0.01', 'decimal', CURRENT_TIMESTAMP)"))
        await s.commit()
    clear_cache()
    await WRITER.start()
    await WRITER.submit(_buy(mid, oids[0], uid, shares="10", accept_any_slippage=True))
    async with async_session_maker() as s:
        cash_before = (await s.get(User, uid)).cash
    await WRITER.submit(_sell(mid, oids[0], uid, shares="10", accept_any_slippage=True))
    async with async_session_maker() as s:
        tx = (await s.execute(select(Transaction).where(
            Transaction.type == TransactionType.SELL))).scalars().first()
        assert tx.fee == (tx.gross * Decimal("0.01")).quantize(Decimal("0.000001"))
        u = await s.get(User, uid)
        assert u.cash == cash_before + tx.gross - tx.fee


@pytest.mark.asyncio
async def test_sell_market_total_insufficient():
    """内存 q 不足时拒绝（异常状态守护，与老路径同文案）。"""
    mid, oids = await _seed_market(shares=("2", "0"))
    uid = await _seed_user()
    await WRITER.start()
    # 用户没持仓也会先撞总量检查？不——总量 2 ≥ 卖 1，会走到持仓检查。
    # 构造总量不足：直接卖 5 > 总量 2
    with pytest.raises(HTTPException) as ei:
        await WRITER.submit(_sell(mid, oids[0], uid, shares="5", accept_any_slippage=True))
    assert ei.value.detail == "市场总份额不足（异常状态）"
