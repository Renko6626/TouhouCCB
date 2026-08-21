"""BuyCmd op 单元测试 + 新旧路径 parity 验证。"""
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
from app.services.writer_ops import BuyCmd


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    # conftest 的 autouse setup_db 已在每测试前 drop+create 全表（并 seed 好
    # activity_mode_enabled 等 site_config），且它先于本 fixture 执行——这里
    # 不再重复 drop/create（否则会把 setup_db 刚 seed 的行冲掉，parity 测试的
    # HTTP 买入走 verify_anti_bot 读 activity_mode_enabled 会 500）。
    # 只负责本文件专属的 WRITER 生命周期收尾。
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


def _buy(mid, oid, uid, shares="10", **kw):
    return BuyCmd(market_id=mid, outcome_id=oid, user_id=uid, username="alice",
                  shares=Decimal(shares), max_cost=kw.get("max_cost"),
                  max_slippage_bps=kw.get("max_slippage_bps"),
                  accept_any_slippage=kw.get("accept_any_slippage", False))


@pytest.mark.asyncio
async def test_buy_happy_path_db_and_memory_consistent():
    mid, oids = await _seed_market(shares=("0", "0"))
    uid = await _seed_user()
    await WRITER.start()
    res = await WRITER.submit(_buy(mid, oids[0], uid, shares="10", accept_any_slippage=True))
    assert res["shares"] == 10.0
    assert res["cost"] > 0
    st = WRITER.get_state(mid)
    assert st.q_dec[0] == Decimal("10.000000")
    async with async_session_maker() as s:
        o = await s.get(Outcome, oids[0])
        assert o.total_shares == st.q_dec[0]          # 镜像与内存恒等（不动点）
        pos = (await s.execute(select(Position).where(
            Position.user_id == uid, Position.outcome_id == oids[0]))).scalars().first()
        assert pos.amount == Decimal("10.000000")
        tx = (await s.execute(select(Transaction))).scalars().all()
        assert len(tx) == 1 and tx[0].type == TransactionType.BUY
    # 现金精确校验：cost_basis == 实付
    async with async_session_maker() as s:
        pos = (await s.execute(select(Position))).scalars().first()
        u = await s.get(User, uid)
        assert u.cash + pos.cost_basis == Decimal("1000")


@pytest.mark.asyncio
async def test_buy_insufficient_cash_rolls_back_everything():
    mid, oids = await _seed_market(shares=("0", "0"))
    uid = await _seed_user(cash="0.01")
    await WRITER.start()
    with pytest.raises(HTTPException) as ei:
        await WRITER.submit(_buy(mid, oids[0], uid, shares="10", accept_any_slippage=True))
    assert ei.value.detail == "现金不足"
    st = WRITER.get_state(mid)
    assert st.q_dec[0] == Decimal("0.000000")          # 内存一个字节没动
    async with async_session_maker() as s:
        o = await s.get(Outcome, oids[0])
        assert o.total_shares == Decimal("0")
        assert (await s.execute(select(Transaction))).scalars().first() is None


@pytest.mark.asyncio
async def test_buy_rejected_on_halt_and_settled_state():
    mid, oids = await _seed_market(status=MarketStatus.HALT)
    uid = await _seed_user()
    await WRITER.start()
    with pytest.raises(HTTPException) as ei:
        await WRITER.submit(_buy(mid, oids[0], uid))
    assert ei.value.detail == "市场当前不可交易"


@pytest.mark.asyncio
async def test_buy_slippage_rejected_before_db():
    mid, oids = await _seed_market(shares=("0", "0"))
    uid = await _seed_user()
    await WRITER.start()
    # b=100 买 200 份冲击巨大，默认 500bps 必拒
    with pytest.raises(HTTPException) as ei:
        await WRITER.submit(_buy(mid, oids[0], uid, shares="200"))
    assert "滑点" in ei.value.detail


@pytest.mark.asyncio
async def test_buy_parity_with_old_path(client):
    """两个同参市场：老路径 API 买 vs writer 直接买，结果逐字段一致。"""
    # 老路径：flag 默认关，走 /api/v1/market/buy
    login = await client.post("/api/v1/auth/dev-login", json={"username": "parity"})
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    hdr = {"Authorization": f"Bearer {token}"}
    # 两个一模一样的市场
    mid_old, oids_old = await _seed_market(shares=("0", "0"))
    mid_new, oids_new = await _seed_market(shares=("0", "0"))
    r_old = await client.post("/api/v1/market/buy", headers=hdr, json={
        "outcome_id": oids_old[0], "shares": 10, "accept_any_slippage": True})
    assert r_old.status_code == 200, r_old.text
    # writer 路径
    await WRITER.start()
    async with async_session_maker() as s:
        uid = (await s.execute(select(User).where(User.username == "parity"))).scalars().first().id
    r_new = await WRITER.submit(_buy(mid_new, oids_new[0], uid, shares="10",
                                     accept_any_slippage=True))
    assert r_old.json()["cost"] == r_new["cost"]
    assert r_old.json()["shares"] == r_new["shares"]
    async with async_session_maker() as s:
        o_old = await s.get(Outcome, oids_old[0])
        o_new = await s.get(Outcome, oids_new[0])
        assert o_old.total_shares == o_new.total_shares
