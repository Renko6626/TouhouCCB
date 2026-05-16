"""startup hook _resync_recent_candles 兜底扫测试。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlmodel import delete, select

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.main import _resync_recent_candles
from app.models.base import Market, MarketStatus, Outcome, OutcomeCandle, User


async def _seed_user_market(b=100.0):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        m = Market(title=f"m_{suffix}", status=MarketStatus.TRADING, liquidity_b=b)
        s.add(m)
        await s.flush()
        oids = []
        for i in range(2):
            o = Outcome(market_id=m.id, label=f"opt_{i}", total_shares=Decimal("0"))
            s.add(o)
            await s.flush()
            oids.append(o.id)
        u = User(username=f"u_{suffix}", email=f"{suffix}@t.com",
                 casdoor_id=f"cd_{suffix}", cash=Decimal("100000"))
        s.add(u)
        await s.flush()
        uid = u.id
        await s.commit()
    return m.id, oids, {"Authorization": f"Bearer {create_access_token(uid)}"}


@pytest.mark.asyncio
async def test_resync_fills_missing_recent_candles(client):
    """造一个"丢了的"candle 场景：手工删某些行，跑 resync 应补回。"""
    mid, oids, headers = await _seed_user_market()
    await client.post("/api/v1/market/buy", headers=headers,
                      json={"outcome_id": oids[0], "shares": 1})

    # 手工删除 outcome[0] 的 10s candle 行（模拟"漏写"）
    async with async_session_maker() as s:
        await s.execute(delete(OutcomeCandle).where(
            OutcomeCandle.outcome_id == oids[0],
            OutcomeCandle.interval == "10s",
        ))
        await s.commit()

    # 跑 resync
    await _resync_recent_candles()

    # 应已恢复
    async with async_session_maker() as s:
        rows = (await s.execute(
            select(OutcomeCandle).where(
                OutcomeCandle.outcome_id == oids[0],
                OutcomeCandle.interval == "10s",
            )
        )).scalars().all()
    assert len(rows) >= 1, "resync 应补回 10s candle"


@pytest.mark.asyncio
async def test_resync_idempotent(client):
    """resync 严格幂等：跑 1 次 vs 跑 N 次结果完全相同。"""
    mid, oids, headers = await _seed_user_market()
    await client.post("/api/v1/market/buy", headers=headers,
                      json={"outcome_id": oids[0], "shares": 1})
    await client.post("/api/v1/market/buy", headers=headers,
                      json={"outcome_id": oids[1], "shares": 2})

    async with async_session_maker() as s:
        before = sorted([
            (c.outcome_id, c.interval, c.bucket_start, c.volume_shares, c.n_trades)
            for c in (await s.execute(select(OutcomeCandle))).scalars().all()
        ])

    # 跑 3 次 resync 模拟连续重启场景
    await _resync_recent_candles()
    await _resync_recent_candles()
    await _resync_recent_candles()

    async with async_session_maker() as s:
        after = sorted([
            (c.outcome_id, c.interval, c.bucket_start, c.volume_shares, c.n_trades)
            for c in (await s.execute(select(OutcomeCandle))).scalars().all()
        ])

    # 严格相等：volume_shares 和 n_trades 不能被重复累加（previous double-count bug
    # 的回归预防）
    assert after == before, (
        f"resync 非幂等 — 重启 3 次后数据漂移：\nbefore={before}\nafter={after}"
    )


@pytest.mark.asyncio
async def test_resync_outside_window_not_touched(client):
    """resync 不应触碰窗口外（很久以前）的 candle 行。"""
    mid, oids, headers = await _seed_user_market()
    await client.post("/api/v1/market/buy", headers=headers,
                      json={"outcome_id": oids[0], "shares": 1})

    # 手工塞一个"很久以前"的 candle（2 年前），模拟 resync 窗口外的历史数据
    ancient = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    async with async_session_maker() as s:
        s.add(OutcomeCandle(
            outcome_id=oids[0], interval="1h", bucket_start=ancient,
            open_price=Decimal("0.5"), high_price=Decimal("0.5"),
            low_price=Decimal("0.5"), close_price=Decimal("0.5"),
            volume_shares=Decimal("99"), n_trades=42,
        ))
        await s.commit()

    await _resync_recent_candles()

    async with async_session_maker() as s:
        ancient_row = (await s.execute(
            select(OutcomeCandle).where(
                OutcomeCandle.outcome_id == oids[0],
                OutcomeCandle.interval == "1h",
                OutcomeCandle.bucket_start == ancient,
            )
        )).scalars().first()

    assert ancient_row is not None, "resync 不该删窗口外的桶"
    assert ancient_row.volume_shares == Decimal("99.000000")
    assert ancient_row.n_trades == 42
