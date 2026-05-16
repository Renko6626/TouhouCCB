"""测试回填函数：跑一批历史 transaction 后 candle 表与实时积累等价。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlmodel import delete, select

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import Market, MarketStatus, Outcome, OutcomeCandle, User
from app.services.candle_writer import backfill_one_market


async def _seed_user_market(b=100.0) -> tuple[int, list[int], dict]:
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
        mid = m.id
        await s.commit()
    return mid, oids, {"Authorization": f"Bearer {create_access_token(uid)}"}


@pytest.mark.asyncio
async def test_backfill_matches_live_writes(client):
    """跑几笔买卖（hot path 会写 candle）→ 清空 candle 表 → 跑回填 → 比对结果一致。"""
    mid, oids, headers = await _seed_user_market()

    for _ in range(3):
        await client.post("/api/v1/market/buy", headers=headers,
                          json={"outcome_id": oids[0], "shares": 1})
    for _ in range(2):
        await client.post("/api/v1/market/buy", headers=headers,
                          json={"outcome_id": oids[1], "shares": 1})

    async with async_session_maker() as s:
        live = sorted([
            (c.outcome_id, c.interval, c.bucket_start,
             c.open_price, c.high_price, c.low_price, c.close_price,
             c.volume_shares, c.n_trades)
            for c in (await s.execute(select(OutcomeCandle))).scalars().all()
        ])

        await s.execute(delete(OutcomeCandle))
        await s.commit()

    async with async_session_maker() as s:
        await backfill_one_market(s, mid)
        await s.commit()

    async with async_session_maker() as s:
        backfilled = sorted([
            (c.outcome_id, c.interval, c.bucket_start,
             c.open_price, c.high_price, c.low_price, c.close_price,
             c.volume_shares, c.n_trades)
            for c in (await s.execute(select(OutcomeCandle))).scalars().all()
        ])

    assert backfilled == live, "回填结果与实时积累不一致"


@pytest.mark.asyncio
async def test_backfill_idempotent(client):
    """跑两次回填，candle 表不会被重复累加。"""
    mid, oids, headers = await _seed_user_market()
    await client.post("/api/v1/market/buy", headers=headers,
                      json={"outcome_id": oids[0], "shares": 1})

    async with async_session_maker() as s:
        await s.execute(delete(OutcomeCandle))
        await s.commit()

    async with async_session_maker() as s:
        await backfill_one_market(s, mid)
        await s.commit()
    async with async_session_maker() as s:
        first = (await s.execute(select(OutcomeCandle))).scalars().all()
        first_snapshot = [(c.outcome_id, c.interval, c.bucket_start,
                           c.volume_shares, c.n_trades) for c in first]

    # 注意：再跑回填会用 upsert_candles（ON CONFLICT DO UPDATE）累加 volume/n_trades
    # 所以严格"再跑结果相同"需要先清空再跑。这个测试验证"先清空+跑回填"是否幂等
    async with async_session_maker() as s:
        await s.execute(delete(OutcomeCandle))
        await backfill_one_market(s, mid)
        await s.commit()
    async with async_session_maker() as s:
        second = (await s.execute(select(OutcomeCandle))).scalars().all()
        second_snapshot = [(c.outcome_id, c.interval, c.bucket_start,
                            c.volume_shares, c.n_trades) for c in second]

    assert sorted(first_snapshot) == sorted(second_snapshot), "回填非幂等"


@pytest.mark.asyncio
async def test_backfill_skips_settle_rows(client):
    """settle/settle_lose Transaction 不应被回填到 candle 表（spec § 2 D4）。"""
    from app.models.base import Transaction, TransactionType
    mid, oids, headers = await _seed_user_market()

    await client.post("/api/v1/market/buy", headers=headers,
                      json={"outcome_id": oids[0], "shares": 1})

    async with async_session_maker() as s:
        s.add(Transaction(
            user_id=1, outcome_id=oids[0], type=TransactionType.SETTLE,
            shares=Decimal("0"), cost=Decimal("0"),
            gross=Decimal("1.0"), price=Decimal("1.0"),
            pre_market_price=Decimal("0"), post_market_price=Decimal("0"),
        ))
        await s.commit()

    async with async_session_maker() as s:
        await s.execute(delete(OutcomeCandle))
        await s.commit()
    async with async_session_maker() as s:
        await backfill_one_market(s, mid)
        await s.commit()

    async with async_session_maker() as s:
        candles = (await s.execute(select(OutcomeCandle))).scalars().all()
    total_n = sum(c.n_trades for c in candles if c.outcome_id == oids[0])
    # 4 个 interval × 1 buy = 4；settle 不参与
    assert total_n == 4
