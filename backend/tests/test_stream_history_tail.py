"""snapshot.history_tail：writer on 走 ring / writer off 走 DB，两路都要有。

模式沿用 tests/test_stream_build_hash.py（本仓库唯一既有的 tests/test_stream_*.py
用例）：直接调 _build_snapshot(db, market_id) 断言 dict 字段，不走真实 SSE HTTP
流——真实开一条 SSE 连接再"读首包后断开"在本仓库的 httpx AsyncClient +
ASGITransport 组合下会挂起（StreamingResponse 的服务端 generator 与客户端提前
断开之间的协作取消在这套 transport 里不干净），直接测 _build_snapshot 更稳定，
且是这次改动实际新增代码的落点。
"""
import time
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio

from app.api.v1.stream import _build_snapshot
from app.core.database import async_session_maker
from app.models.base import Market, MarketStatus, Outcome, OutcomeCandle
from app.services.history_ring import seal_boundary
from app.services.market_writer import WRITER


@pytest_asyncio.fixture(autouse=True)
async def _teardown():
    yield
    await WRITER.stop()


async def _seed(now_epoch: int):
    # 注意：seed 与请求之间若恰好跨过 1h 封存边界会 flaky（概率 ~1/3600）。
    # 边界紧邻时把种子桶放到"新边界后"仍成立，这里接受该极小概率重跑。
    boundary = seal_boundary("1m", now_epoch)
    ts = datetime.fromtimestamp(boundary + 60, tz=timezone.utc)   # 边界之后 → 属于尾巴
    async with async_session_maker() as s:
        m = Market(title="t", description="", liquidity_b=100.0, status=MarketStatus.TRADING)
        s.add(m); await s.flush()
        o = Outcome(market_id=m.id, label="a", total_shares=Decimal("0"))
        s.add(o); await s.flush()
        s.add(OutcomeCandle(
            outcome_id=o.id, interval="1m", bucket_start=ts,
            open_price=Decimal("0.5"), high_price=Decimal("0.6"),
            low_price=Decimal("0.5"), close_price=Decimal("0.6"),
            volume_shares=Decimal("2"), n_trades=1, updated_at=ts,
        ))
        await s.commit()
        return int(m.id), int(o.id), boundary


@pytest.mark.asyncio
async def test_tail_from_db_when_writer_off():
    mid, oid, boundary = await _seed(int(time.time()))
    async with async_session_maker() as db:
        snap = await _build_snapshot(db, mid)
    tail = snap["history_tail"][str(oid)]["1m"]
    assert tail["t0"] == boundary
    assert 1 in tail["t"]                       # boundary+60 → 桶序号 1
    assert set(snap["history_tail"][str(oid)].keys()) == {"10s", "1m", "15m", "1h"}


@pytest.mark.asyncio
async def test_tail_from_ring_when_writer_on():
    mid, oid, boundary = await _seed(int(time.time()))
    await WRITER.start()                        # Task 3：启动回灌 ring
    async with async_session_maker() as db:
        snap = await _build_snapshot(db, mid)
    tail = snap["history_tail"][str(oid)]["1m"]
    assert tail["t0"] == boundary and 1 in tail["t"]
