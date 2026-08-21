"""GET /history/ 三道防线 + 编码 + 缓存头测试。"""
import time
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio

from app.api.v1 import history as history_api
from app.core.database import async_session_maker
from app.models.base import Market, MarketStatus, Outcome, OutcomeCandle
from app.services.candle_flusher import CANDLE_FLUSHER
from app.services.market_writer import WRITER


@pytest_asyncio.fixture(autouse=True)
async def _teardown():
    yield
    await WRITER.stop()
    CANDLE_FLUSHER._pending.clear()
    # 进程内 LRU 是模块级全局，跨测试常驻；每测试前 conftest 会 drop_all+create_all
    # 全表，sqlite 无 AUTOINCREMENT 时主键从 1 重新分配，不清理会导致下一测试的
    # outcome_id 撞上前一测试缓存的 (outcome_id, interval, segment_epoch) key，
    # 命中过期缓存条目——这是纯测试环境的 ID 复用假象，生产 ID 单调递增不会撞。
    history_api._lru.clear()


def _seg(epoch: int, seg_len: int) -> int:
    return epoch - epoch % seg_len


async def _seed_outcome_with_candle(epoch: int, interval="1m") -> int:
    ts = datetime.fromtimestamp(epoch, tz=timezone.utc)
    async with async_session_maker() as s:
        m = Market(title="t", description="", liquidity_b=100.0, status=MarketStatus.TRADING)
        s.add(m); await s.flush()
        o = Outcome(market_id=m.id, label="a", total_shares=Decimal("0"))
        s.add(o); await s.flush()
        oid = int(o.id)
        s.add(OutcomeCandle(
            outcome_id=oid, interval=interval, bucket_start=ts,
            open_price=Decimal("0.5"), high_price=Decimal("0.6"),
            low_price=Decimal("0.5"), close_price=Decimal("0.6"),
            volume_shares=Decimal("2"), n_trades=1, updated_at=ts,
        ))
        await s.commit()
        return oid


@pytest.mark.asyncio
async def test_inflight_segment_404(client):
    """防线 1：段末尾在未来 → 404，绝不吐进行中的段。"""
    oid = await _seed_outcome_with_candle(int(time.time()) - 60)
    cur = _seg(int(time.time()), 3600)
    resp = await client.get(f"/history/o/{oid}/1m/{cur}.json")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_sealed_segment_from_db_200_immutable(client):
    """防线 3（writer off / 超窗）：已封存 + flusher 无 pending → DB 供数 + immutable。"""
    epoch = int(time.time()) - 2 * 3600           # 上上个 1h 段内
    epoch -= epoch % 60
    oid = await _seed_outcome_with_candle(epoch)
    seg = _seg(epoch, 3600)
    resp = await client.get(f"/history/o/{oid}/1m/{seg}.json")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"
    body = resp.json()
    assert body["t0"] == seg and body["step"] == 60 and body["n_buckets"] == 60
    idx = (epoch - seg) // 60
    assert idx in body["t"]
    assert body["c"][body["t"].index(idx)] == round(0.6 * 1e8)


@pytest.mark.asyncio
async def test_unflushed_segment_404(client):
    """防线 3：flusher 高水位覆盖段范围 → 404（未落库的段绝不固化）。"""
    epoch = int(time.time()) - 2 * 3600
    epoch -= epoch % 60
    oid = await _seed_outcome_with_candle(epoch)
    seg = _seg(epoch, 3600)
    ts = datetime.fromtimestamp(seg + 60, tz=timezone.utc)
    CANDLE_FLUSHER.merge([{
        "outcome_id": oid, "interval": "1m", "bucket_start": ts,
        "open_price": Decimal("0.5"), "high_price": Decimal("0.5"),
        "low_price": Decimal("0.5"), "close_price": Decimal("0.5"),
        "volume_shares": Decimal("1"), "n_trades": 1, "updated_at": ts,
    }])
    resp = await client.get(f"/history/o/{oid}/1m/{seg}.json")
    assert resp.status_code == 404
    CANDLE_FLUSHER._pending.clear()


@pytest.mark.asyncio
async def test_ring_serves_in_window_segment_without_db(client):
    """防线 2：writer on 且段在 ring 窗口内 → 一律 ring 供数（哪怕 DB 是空的）。"""
    epoch = int(time.time()) - 700                # 上一个 10min 段内（10s 档）
    epoch -= epoch % 10
    oid = await _seed_outcome_with_candle(epoch, interval="10s")
    await WRITER.start()
    st = WRITER.get_state(WRITER.market_id_for_outcome(oid))
    assert st is not None and oid in st.rings     # Task 3 已回灌
    # 删掉 DB 行证明走的是 ring
    from sqlalchemy import delete
    async with async_session_maker() as s:
        await s.execute(delete(OutcomeCandle).where(OutcomeCandle.outcome_id == oid))
        await s.commit()
    seg = _seg(epoch, 600)
    resp = await client.get(f"/history/o/{oid}/10s/{seg}.json")
    assert resp.status_code == 200
    assert (epoch - seg) // 10 in resp.json()["t"]


@pytest.mark.asyncio
async def test_validation_404s(client):
    oid = await _seed_outcome_with_candle(int(time.time()) - 7200)
    past = _seg(int(time.time()) - 7200, 3600)
    assert (await client.get(f"/history/o/{oid}/5m/{past}.json")).status_code == 404       # interval 白名单
    assert (await client.get(f"/history/o/{oid}/1m/{past + 61}.json")).status_code == 404  # epoch 未对齐
    assert (await client.get(f"/history/o/999999/1m/{past}.json")).status_code == 404      # outcome 不存在


@pytest.mark.asyncio
async def test_empty_sealed_segment_is_200(client):
    """无成交的已封存段是合法 200（空数组编码）——空也是不可变事实。"""
    oid = await _seed_outcome_with_candle(int(time.time()) - 7200)
    older = _seg(int(time.time()) - 8 * 3600, 3600)
    resp = await client.get(f"/history/o/{oid}/1m/{older}.json")
    assert resp.status_code == 200
    assert resp.json()["t"] == []
