"""MarketWriter 命令循环——双道背压 + 自愈单元测试（不走 HTTP，不需要 client fixture）。"""
import asyncio
from dataclasses import dataclass
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlmodel import SQLModel

from app.core.database import engine, async_session_maker
from app.models.base import Market, MarketStatus, Outcome
from app.services.market_writer import WRITER, MarketState, OpOutcome


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
        await conn.run_sync(SQLModel.metadata.create_all)
    yield
    await WRITER.stop()


async def _seed_market(status=MarketStatus.TRADING, shares=("3.5", "0")) -> tuple[int, list[int]]:
    async with async_session_maker() as s:
        m = Market(title="t", description="", liquidity_b=100.0, status=status)
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


@dataclass
class FakeCmd:
    market_id: int
    behavior: str = "ok"          # ok | http_error | crash_after_commit | slow


def _make_fake_op(db_mutator=None):
    async def op(state: MarketState, cmd: FakeCmd) -> OpOutcome:
        if cmd.behavior == "http_error":
            raise HTTPException(status_code=400, detail="业务拒绝")
        if cmd.behavior == "slow":
            await asyncio.sleep(30)
        if cmd.behavior == "crash_after_commit":
            if db_mutator:
                await db_mutator()      # 模拟「commit 已成功」：直接改 DB
            raise RuntimeError("boom after commit")
        return OpOutcome(
            response={"ok": True},
            new_q_dec=[state.q_dec[0] + Decimal("1"), state.q_dec[1]],
        )
    return op


@pytest.mark.asyncio
async def test_submit_ok_applies_memory_after_op():
    mid, _ = await _seed_market()
    await WRITER.start()
    WRITER.register_op(FakeCmd, _make_fake_op())
    res = await WRITER.submit(FakeCmd(market_id=mid))
    assert res == {"ok": True}
    st = WRITER.get_state(mid)
    assert st.q_dec[0] == Decimal("4.500000")   # 3.5 + 1
    assert st.q[0] == float(Decimal("4.500000"))


@pytest.mark.asyncio
async def test_http_error_propagates_and_memory_untouched():
    mid, _ = await _seed_market()
    await WRITER.start()
    WRITER.register_op(FakeCmd, _make_fake_op())
    with pytest.raises(HTTPException) as ei:
        await WRITER.submit(FakeCmd(market_id=mid, behavior="http_error"))
    assert ei.value.status_code == 400
    assert WRITER.get_state(mid).q_dec[0] == Decimal("3.500000")


@pytest.mark.asyncio
async def test_queue_full_raises_429(monkeypatch):
    mid, _ = await _seed_market()
    await WRITER.start()
    WRITER.register_op(FakeCmd, _make_fake_op())
    monkeypatch.setattr(WRITER, "SUBMIT_TIMEOUT", 0.2)
    # 先塞一个 slow 占住 consumer，再灌满队列
    slow = asyncio.create_task(_submit_swallow(FakeCmd(market_id=mid, behavior="slow")))
    await asyncio.sleep(0.05)
    q = WRITER._queues[mid]
    fillers = []
    while not q.full():
        fillers.append(asyncio.create_task(_submit_swallow(FakeCmd(market_id=mid))))
        await asyncio.sleep(0)
    with pytest.raises(HTTPException) as ei:
        await WRITER.submit(FakeCmd(market_id=mid))
    assert ei.value.status_code == 429
    slow.cancel()
    for f in fillers:
        f.cancel()


async def _submit_swallow(cmd):
    try:
        await WRITER.submit(cmd)
    except (HTTPException, asyncio.CancelledError):
        pass


@pytest.mark.asyncio
async def test_submit_timeout_returns_503(monkeypatch):
    mid, _ = await _seed_market()
    await WRITER.start()
    WRITER.register_op(FakeCmd, _make_fake_op())
    monkeypatch.setattr(WRITER, "SUBMIT_TIMEOUT", 0.1)
    with pytest.raises(HTTPException) as ei:
        await WRITER.submit(FakeCmd(market_id=mid, behavior="slow"))
    assert ei.value.status_code == 503
    assert "结果未知" in ei.value.detail   # spec § 4.3：措辞必须是结果未知


@pytest.mark.asyncio
async def test_unexpected_exception_self_heals_from_mirror():
    """commit 后异常 → 内存从镜像重读（spec § 4.4 异常策略）。"""
    from sqlalchemy import update
    from app.core.database import async_session_maker
    from app.models.base import Outcome

    mid, oids = await _seed_market()
    await WRITER.start()

    async def mutate_db():
        async with async_session_maker() as s:
            await s.execute(update(Outcome).where(Outcome.id == oids[0])
                            .values(total_shares=Decimal("99.000000")))
            await s.commit()

    WRITER.register_op(FakeCmd, _make_fake_op(db_mutator=mutate_db))
    with pytest.raises(HTTPException) as ei:
        await WRITER.submit(FakeCmd(market_id=mid, behavior="crash_after_commit"))
    assert ei.value.status_code == 500
    await asyncio.sleep(0.1)   # 让 consumer 完成 reload
    st = WRITER.get_state(mid)
    assert st.q_dec[0] == Decimal("99.000000")   # 重读到了 DB 的真值
    assert not st.unavailable
    # 自愈后还能继续服务
    res = await WRITER.submit(FakeCmd(market_id=mid))
    assert res == {"ok": True}
