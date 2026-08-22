"""MarketState.rings 接线测试：启动回灌 / 成交喂入 / reload 重建。"""
import time
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlmodel import SQLModel

from app.core.database import engine, async_session_maker
from app.models.base import Market, MarketStatus, Outcome, OutcomeCandle, User
from app.services.history_ring import seal_boundary
from app.services.market_writer import WRITER
from app.services.writer_ops import BuyCmd


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


async def _seed_user(cash="1000") -> int:
    async with async_session_maker() as s:
        u = User(username="alice", casdoor_id="cas_alice", cash=Decimal(cash))
        s.add(u)
        await s.flush()
        uid = u.id
        await s.commit()
        return uid


def _candle_row(oid: int, interval: str, epoch: int, price="0.5", v="1", n=1):
    ts = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return OutcomeCandle(
        outcome_id=oid, interval=interval, bucket_start=ts,
        open_price=Decimal(price), high_price=Decimal(price),
        low_price=Decimal(price), close_price=Decimal(price),
        volume_shares=Decimal(v), n_trades=n, updated_at=ts,
    )


@pytest.mark.asyncio
async def test_start_backfills_rings_from_mirror():
    mid, oids = await _seed_market()
    now = int(time.time())
    in_window = (now - now % 60) - 120            # 1m 档窗口内
    out_of_window = (now - now % 60) - 25 * 3600  # 超出 1m 档 24h 窗口
    async with async_session_maker() as s:
        s.add(_candle_row(oids[0], "1m", in_window))
        s.add(_candle_row(oids[0], "1m", out_of_window))
        await s.commit()
    await WRITER.start()
    ring = WRITER.get_state(mid).rings[oids[0]]
    seg_epoch = in_window - in_window % 3600
    assert (in_window - seg_epoch) // 60 in ring.get_segment("1m", seg_epoch)["t"]
    old_seg = out_of_window - out_of_window % 3600
    assert ring.get_segment("1m", old_seg)["t"] == []   # 超窗行不回灌


@pytest.mark.asyncio
async def test_buy_feeds_rings_all_outcomes():
    mid, oids = await _seed_market(shares=("0", "0"))
    uid = await _seed_user()
    await WRITER.start()
    await WRITER.submit(BuyCmd(
        market_id=mid, outcome_id=oids[0], user_id=uid, username="alice",
        shares=Decimal("10"), max_cost=None, max_slippage_bps=None,
        accept_any_slippage=True,
    ))
    st = WRITER.get_state(mid)
    now = int(time.time())
    for oid in oids:   # 被交易 outcome 与联动 outcome 都有价格桶
        t = st.rings[oid].tail("10s", now)
        assert t["t"], f"outcome {oid} ring 未被喂入"
    traded = st.rings[oids[0]].tail("10s", now)
    linked = st.rings[oids[1]].tail("10s", now)
    assert sum(traded["trades"]) == 1 and sum(traded["v"]) == 10.0
    assert sum(linked["trades"]) == 0 and sum(linked["v"]) == 0.0


@pytest.mark.asyncio
async def test_reload_state_rebuilds_rings_from_mirror():
    mid, oids = await _seed_market()
    await WRITER.start()
    now = int(time.time())
    epoch = now - now % 60
    async with async_session_maker() as s:
        s.add(_candle_row(oids[0], "1m", epoch, price="0.9"))
        await s.commit()
    await WRITER.reload_state(mid)
    ring = WRITER.get_state(mid).rings[oids[0]]
    seg = ring.get_segment("1m", epoch - epoch % 3600)
    assert round(0.9 * 1e8) in seg["c"]


@pytest.mark.asyncio
async def test_reload_state_flushes_pending_candles_first():
    """审计 M5：reload 前 flusher 里 ≤5s 未落库的行必须先进 DB，否则新 ring 少这几笔。"""
    from app.services.candle_flusher import CANDLE_FLUSHER
    mid, oids = await _seed_market(shares=("0", "0"))
    uid = await _seed_user()
    await WRITER.start()
    await WRITER.submit(BuyCmd(
        market_id=mid, outcome_id=oids[0], user_id=uid, username="alice",
        shares=Decimal("10"), max_cost=None, max_slippage_bps=None,
        accept_any_slippage=True,
    ))
    assert CANDLE_FLUSHER._pending, "成交行应先停在 flusher（测试环境 flusher 未启动）"
    now = int(time.time())
    await WRITER.reload_state(mid)
    assert not CANDLE_FLUSHER._pending
    t = WRITER.get_state(mid).rings[oids[0]].tail("10s", now)
    assert sum(t["v"]) == 10.0, "重建后的 ring 丢了在途成交"
