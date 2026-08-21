"""CandleFlusher 单元测试：内存合并 + 5s 批量落库 + 失败回炉（不 double-count）。"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlmodel import SQLModel, select

from app.core.database import engine, async_session_maker
from app.models.base import Market, MarketStatus, Outcome, OutcomeCandle
from app.services.candle_flusher import CANDLE_FLUSHER


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
        await conn.run_sync(SQLModel.metadata.create_all)
    yield


@pytest.fixture(autouse=True)
def _reset_flusher():
    CANDLE_FLUSHER._pending.clear()
    yield


def _row(oid: int, interval="10s", bucket=None, o="0.5", h="0.6", l="0.5", c="0.6", v="1", n=1):
    return {
        "outcome_id": oid, "interval": interval,
        "bucket_start": bucket or datetime(2026, 8, 21, 0, 0, 0, tzinfo=timezone.utc),
        "open_price": Decimal(o), "high_price": Decimal(h),
        "low_price": Decimal(l), "close_price": Decimal(c),
        "volume_shares": Decimal(v), "n_trades": n,
        "updated_at": datetime(2026, 8, 21, 0, 0, 1, tzinfo=timezone.utc),
    }


async def _seed_outcome() -> int:
    async with async_session_maker() as s:
        m = Market(title="t", description="", liquidity_b=100.0, status=MarketStatus.TRADING, tags="")
        s.add(m); await s.flush()
        o = Outcome(market_id=m.id, label="a", total_shares=Decimal("0"))
        s.add(o); await s.flush()
        oid = o.id
        await s.commit()
        return oid


def test_merge_order_aware():
    """同桶两笔：open 保留首笔，close 取末笔，high/low 取极值，vol/n 累加。"""
    CANDLE_FLUSHER.merge([_row(1, o="0.5", h="0.6", l="0.5", c="0.6", v="1", n=1)])
    CANDLE_FLUSHER.merge([_row(1, o="0.6", h="0.7", l="0.4", c="0.55", v="2", n=1)])
    assert CANDLE_FLUSHER.pending_count() == 1
    merged = next(iter(CANDLE_FLUSHER._pending.values()))
    assert merged["open_price"] == Decimal("0.5")
    assert merged["close_price"] == Decimal("0.55")
    assert merged["high_price"] == Decimal("0.7")
    assert merged["low_price"] == Decimal("0.4")
    assert merged["volume_shares"] == Decimal("3")
    assert merged["n_trades"] == 2


@pytest.mark.asyncio
async def test_flush_once_writes_and_drains():
    oid = await _seed_outcome()
    CANDLE_FLUSHER.merge([_row(oid)])
    n = await CANDLE_FLUSHER.flush_once()
    assert n == 1
    assert CANDLE_FLUSHER.pending_count() == 0
    async with async_session_maker() as s:
        rows = (await s.execute(select(OutcomeCandle))).scalars().all()
        assert len(rows) == 1
        assert rows[0].volume_shares == Decimal("1")
    # 再 flush 一次必须是 no-op（否则 upsert 累加语义会 double-count）
    assert await CANDLE_FLUSHER.flush_once() == 0
    async with async_session_maker() as s:
        rows = (await s.execute(select(OutcomeCandle))).scalars().all()
        assert rows[0].volume_shares == Decimal("1")


@pytest.mark.asyncio
async def test_flush_failure_remerges(monkeypatch):
    oid = await _seed_outcome()
    CANDLE_FLUSHER.merge([_row(oid, c="0.6", v="1", n=1)])

    async def boom(db, rows):
        raise RuntimeError("db down")
    monkeypatch.setattr("app.services.candle_flusher.upsert_candles", boom)
    assert await CANDLE_FLUSHER.flush_once() == 0
    assert CANDLE_FLUSHER.pending_count() == 1    # 这批回炉了
    monkeypatch.undo()

    # 失败期间又来一笔同桶：回炉行是"较早"方，open 用它的
    CANDLE_FLUSHER.merge([_row(oid, o="0.6", c="0.7", v="2", n=1)])
    merged = next(iter(CANDLE_FLUSHER._pending.values()))
    assert merged["open_price"] == Decimal("0.5")
    assert merged["close_price"] == Decimal("0.7")
    assert merged["volume_shares"] == Decimal("3")


def test_oldest_pending_bucket_none_when_empty():
    assert CANDLE_FLUSHER.oldest_pending_bucket() is None


def test_oldest_pending_bucket_returns_min_bucket_start():
    from datetime import datetime, timezone
    early = datetime(2026, 8, 21, 0, 0, 0, tzinfo=timezone.utc)
    late = datetime(2026, 8, 21, 0, 10, 0, tzinfo=timezone.utc)
    CANDLE_FLUSHER.merge([_row(1, bucket=late)])
    CANDLE_FLUSHER.merge([_row(1, bucket=early)])
    assert CANDLE_FLUSHER.oldest_pending_bucket() == early
