from tests.test_account_events import private_events
"""CloseCmd/ResumeCmd/ResolveCmd op 单元测试（fixture 拷贝自 test_writer_buy.py，文件间不共享 import）。"""
from decimal import Decimal
import asyncio

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlmodel import select

from app.core.database import async_session_maker
from app.models.base import (
    Market, MarketStatus, Outcome, Position, Transaction, TransactionType, User,
)
from app.schemas.trading_context import GroupKey
from app.services import trading_prices
from app.services.market_writer import WRITER
from app.services.writer_ops import BuyCmd, CloseCmd, ResolveCmd, ResumeCmd, SellCmd


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db(monkeypatch):
    registry = trading_prices.PricingRegistry(async_session_maker)
    registry.configure(memory_products=frozenset({'lmsr'}))
    monkeypatch.setattr(trading_prices, 'REGISTRY', registry)
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
    key = GroupKey(product='lmsr', id=mid)
    assert (await trading_prices.REGISTRY.read_groups([key]))[0].status == MarketStatus.HALT
    async with async_session_maker() as s:
        assert (await s.get(Market, mid)).status == MarketStatus.HALT
    with pytest.raises(HTTPException):
        await WRITER.submit(_buy(mid, oids[0], uid, accept_any_slippage=True))
    await WRITER.submit(ResumeCmd(market_id=mid))
    assert WRITER.get_state(mid).status == MarketStatus.TRADING
    assert (await trading_prices.REGISTRY.read_groups([key]))[0].status == MarketStatus.TRADING
    res = await WRITER.submit(_buy(mid, oids[0], uid, shares="1", accept_any_slippage=True))
    assert res["shares"] == 1.0
    assert (await trading_prices.REGISTRY.read_groups([key]))[0].q_dec[0] == '4.500000'
    await WRITER.submit(SellCmd(market_id=mid, outcome_id=oids[0], user_id=uid,
                               username='alice', shares=Decimal('1'), min_proceeds=None,
                               max_slippage_bps=None, accept_any_slippage=True))
    assert (await trading_prices.REGISTRY.read_groups([key]))[0].q_dec[0] == '3.500000'


@pytest.mark.asyncio
async def test_resume_requires_halt():
    mid, _ = await _seed_market()
    await WRITER.start()
    with pytest.raises(HTTPException) as ei:
        await WRITER.submit(ResumeCmd(market_id=mid))
    assert "不在熔断状态" in ei.value.detail


@pytest.mark.asyncio
async def test_resolve_pays_winner_deletes_positions_and_settles(private_events):
    mid, oids = await _seed_market(shares=("0", "0"))
    uid_w = await _seed_user()          # 买赢家
    uid_l = await _seed_user2()         # 买输家
    await WRITER.start()
    await WRITER.submit(_buy(mid, oids[0], uid_w, shares="10", accept_any_slippage=True))
    await WRITER.submit(_buy(mid, oids[1], uid_l, shares="10", accept_any_slippage=True))
    async with async_session_maker() as s:
        cash_w_before = (await s.get(User, uid_w)).cash
    queue = private_events(uid_w)
    res = await WRITER.submit(ResolveCmd(
        market_id=mid, winning_outcome_id=oids[0], payout=Decimal("1"), admin_id=uid_w))
    assert queue.get_nowait()[1]["reason"] == "lmsr"
    assert queue.empty()
    assert res.status == MarketStatus.SETTLED
    assert res.winning_outcome_id == oids[0]
    assert res.total_payout == Decimal("10.000000")
    assert res.settled_positions == 2
    st = WRITER.get_state(mid)
    assert st.status == MarketStatus.SETTLED       # 状态留内存，后续交易被拒
    assert (await trading_prices.REGISTRY.read_groups([GroupKey(product='lmsr', id=mid)]))[0].status == MarketStatus.SETTLED
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


@pytest.mark.asyncio
async def test_account_hint_follows_writer_projection(private_events):
    from app.services.credit.trading_context import read_trading_context
    from app.services.writer_ops import op_buy
    mid, oids = await _seed_market(shares=('0', '0'))
    uid = await _seed_user()
    await WRITER.start()
    key = GroupKey(product='lmsr', id=mid)
    queue = private_events(uid)
    committed, proceed = asyncio.Event(), asyncio.Event()
    async def delayed_apply(state, cmd):
        outcome = await op_buy(state, cmd)  # Committed, but projection is still old.
        committed.set()
        await proceed.wait()
        return outcome
    WRITER.register_op(BuyCmd, delayed_apply)
    writing = asyncio.create_task(WRITER.submit(_buy(mid, oids[0], uid, shares='1', accept_any_slippage=True)))
    await committed.wait()
    assert queue.empty()
    async def context():
        async with async_session_maker() as db:
            return await read_trading_context(db, uid, key, registry=trading_prices.REGISTRY)
    try:
        public = await trading_prices.REGISTRY.read_groups([key], require_coverage=False)
        assert public[0].q_dec[0] == '0.000000'
        assert queue.empty()
    finally:
        proceed.set()
        await writing
    assert queue.get_nowait()[1]['reason'] == 'lmsr'
    assert queue.empty()
    result = await context()
    assert result.account.economic_version == '1'
    assert result.account.lmsr_positions[0].amount == '1.000000'
    assert result.groups[0].q_dec[0] == '1.000000'


@pytest.mark.asyncio
async def test_committed_install_failure_invalidates_before_failed_reload(monkeypatch, caplog, private_events):
    mid, oids = await _seed_market(shares=('0', '0'))
    uid = await _seed_user()
    await WRITER.start()
    registry = trading_prices.REGISTRY
    queue = private_events(uid)
    retried = asyncio.Event()
    attempts = 0
    def fail_install(state):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            retried.set()
        raise RuntimeError('snapshot unavailable')
    with monkeypatch.context() as patch:
        patch.setattr(registry, 'install_committed', fail_install)
        with pytest.raises(HTTPException) as exc:
            await WRITER.submit(_buy(mid, oids[0], uid, shares='1', accept_any_slippage=True))
        assert exc.value.status_code == 500
        await retried.wait()
    assert WRITER.get_state(mid).unavailable
    assert queue.empty()
    assert 'LMSR database committed; snapshot installation failed' in caplog.text
    async with async_session_maker() as db:
        assert (await db.get(Outcome, oids[0])).total_shares == Decimal('1.000000')
    # The previous zero-shares snapshot cannot be used after failed recovery.
    with pytest.raises(HTTPException) as unavailable:
        await registry.read_groups([GroupKey(product='lmsr', id=mid)])
    assert unavailable.value.status_code == 503
    await WRITER.reload_state(mid)
    group = (await registry.read_groups([GroupKey(product='lmsr', id=mid)]))[0]
    assert group.q_dec == ['1.000000', '0.000000']


@pytest.mark.asyncio
@pytest.mark.parametrize('lifecycle', ['register_market', 'reload_state'])
async def test_existing_market_lifecycle_serializes_between_trades(monkeypatch, lifecycle):
    from app.services import market_writer
    from app.services.writer_ops import op_buy
    mid, oids = await _seed_market(shares=('0', '0'))
    uid = await _seed_user()
    await WRITER.start()
    committed, apply_allowed = asyncio.Event(), asyncio.Event()
    loading, load_allowed = asyncio.Event(), asyncio.Event()
    original_load = market_writer._load_one
    calls = 0

    async def delayed_buy(state, cmd):
        nonlocal calls
        calls += 1
        outcome = await op_buy(state, cmd)
        if calls == 1:
            committed.set()
            await apply_allowed.wait()
        return outcome

    async def delayed_load(session, market):
        loading.set()
        await load_allowed.wait()
        return await original_load(session, market)

    WRITER.register_op(BuyCmd, delayed_buy)
    monkeypatch.setattr(market_writer, '_load_one', delayed_load)
    first = asyncio.create_task(WRITER.submit(_buy(mid, oids[0], uid, shares='1', accept_any_slippage=True)))
    await committed.wait()
    maintenance = asyncio.create_task(getattr(WRITER, lifecycle)(mid))
    await asyncio.sleep(0)  # Enqueue maintenance before the next trade.
    second = asyncio.create_task(WRITER.submit(_buy(mid, oids[0], uid, shares='1', accept_any_slippage=True)))
    try:
        assert not loading.is_set()
        apply_allowed.set()
        await loading.wait()
        assert not second.done()
    finally:
        apply_allowed.set()
        load_allowed.set()
        await asyncio.gather(first, maintenance, second)
    assert WRITER.get_state(mid).q_dec[0] == Decimal('2.000000')
    key = GroupKey(product='lmsr', id=mid)
    assert (await trading_prices.REGISTRY.read_groups([key]))[0].q_dec[0] == '2.000000'
    async with async_session_maker() as db:
        assert (await db.get(Outcome, oids[0])).total_shares == Decimal('2.000000')
