"""通过 API 调 buy/sell/settle 验 candle 表副作用。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlmodel import select

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import Market, MarketStatus, Outcome, OutcomeCandle, User


async def _seed_user(cash: Decimal = Decimal("10000")) -> tuple[int, dict]:
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        u = User(
            username=f"u_{suffix}", email=f"{suffix}@t.com",
            casdoor_id=f"cd_{suffix}",
            cash=cash, debt=Decimal("0"),
        )
        s.add(u)
        await s.flush()
        uid = u.id
        await s.commit()
    return uid, {"Authorization": f"Bearer {create_access_token(uid)}"}


async def _seed_admin() -> dict:
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        u = User(
            username=f"admin_{suffix}", email=f"{suffix}@t.com",
            casdoor_id=f"cd_{suffix}",
            cash=Decimal("0"), is_superuser=True,
        )
        s.add(u)
        await s.flush()
        uid = u.id
        await s.commit()
    return {"Authorization": f"Bearer {create_access_token(uid)}"}


async def _seed_market(n_outcomes: int = 2, b: float = 100.0) -> tuple[int, list[int]]:
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        m = Market(title=f"m_{suffix}", status=MarketStatus.TRADING, liquidity_b=b)
        s.add(m)
        await s.flush()
        oids = []
        for i in range(n_outcomes):
            o = Outcome(market_id=m.id, label=f"opt_{i}", total_shares=Decimal("0"))
            s.add(o)
            await s.flush()
            oids.append(o.id)
        await s.commit()
        return m.id, oids


async def _count_candles(outcome_id: int) -> int:
    async with async_session_maker() as s:
        rows = (await s.execute(
            select(OutcomeCandle).where(OutcomeCandle.outcome_id == outcome_id)
        )).scalars().all()
        return len(rows)


@pytest.mark.asyncio
async def test_single_buy_writes_n_times_4_rows(client):
    """一笔 buy → 全市场 N outcome × 4 interval = N×4 行 candle。"""
    mid, oids = await _seed_market(n_outcomes=2)
    _, headers = await _seed_user(cash=Decimal("1000"))

    resp = await client.post(
        f"/api/v1/market/buy",
        headers=headers,
        json={"outcome_id": oids[0], "shares": 1},
    )
    assert resp.status_code == 200, resp.text

    # 每个 outcome 4 行（10s/1m/15m/1h）
    for oid in oids:
        assert await _count_candles(oid) == 4

    # 被交易 outcome 的 volume_shares > 0、n_trades=1；联动 outcome v=0、n=0
    async with async_session_maker() as s:
        traded = (await s.execute(
            select(OutcomeCandle).where(
                OutcomeCandle.outcome_id == oids[0],
                OutcomeCandle.interval == "1m",
            )
        )).scalars().first()
        linked = (await s.execute(
            select(OutcomeCandle).where(
                OutcomeCandle.outcome_id == oids[1],
                OutcomeCandle.interval == "1m",
            )
        )).scalars().first()

    assert traded.n_trades == 1
    assert traded.volume_shares > 0
    assert linked.n_trades == 0
    assert linked.volume_shares == Decimal("0.000000")


@pytest.mark.asyncio
async def test_two_buys_same_bucket_merge(client):
    """同 bucket 两笔 buy → OHLC 合并，V/n 累加。"""
    mid, oids = await _seed_market(n_outcomes=2)
    _, headers = await _seed_user(cash=Decimal("1000"))

    await client.post("/api/v1/market/buy", headers=headers,
                      json={"outcome_id": oids[0], "shares": 1})
    await client.post("/api/v1/market/buy", headers=headers,
                      json={"outcome_id": oids[0], "shares": 1})

    async with async_session_maker() as s:
        # 1m bucket 几乎肯定是同一个（两次买间隔 <1 秒）
        rows = (await s.execute(
            select(OutcomeCandle).where(
                OutcomeCandle.outcome_id == oids[0],
                OutcomeCandle.interval == "1m",
            )
        )).scalars().all()

    assert len(rows) == 1, "两笔 buy 应在同一 1m bucket"
    assert rows[0].n_trades == 2
    assert rows[0].volume_shares == Decimal("2.000000")
    # close_price 应该是第二次的（比第一次高）
    assert rows[0].close_price > rows[0].open_price


@pytest.mark.asyncio
async def test_sell_writes_candle(client):
    """sell 路径同样触发 candle 写入。"""
    mid, oids = await _seed_market(n_outcomes=2)
    _, headers = await _seed_user(cash=Decimal("1000"))

    # 先 buy 建持仓
    await client.post("/api/v1/market/buy", headers=headers,
                      json={"outcome_id": oids[0], "shares": 5})
    candle_count_before = await _count_candles(oids[0])

    # sell
    resp = await client.post("/api/v1/market/sell", headers=headers,
                             json={"outcome_id": oids[0], "shares": 2})
    assert resp.status_code == 200, resp.text

    candle_count_after = await _count_candles(oids[0])
    # 同 bucket 内 → 行数不增，但 n_trades+=1, v+=2
    assert candle_count_after == candle_count_before

    async with async_session_maker() as s:
        row = (await s.execute(
            select(OutcomeCandle).where(
                OutcomeCandle.outcome_id == oids[0],
                OutcomeCandle.interval == "1m",
            )
        )).scalars().first()
    assert row.n_trades == 2  # 1 buy + 1 sell
    assert row.volume_shares == Decimal("7.000000")  # 5 + 2


@pytest.mark.asyncio
async def test_settle_does_not_write_candle(client):
    """resolve_market 触发 settle/settle_lose → candle 表行数不增。"""
    mid, oids = await _seed_market(n_outcomes=2)
    _, headers = await _seed_user(cash=Decimal("1000"))
    admin_headers = await _seed_admin()

    # 一笔 buy 让 candle 表有些数据
    await client.post("/api/v1/market/buy", headers=headers,
                      json={"outcome_id": oids[0], "shares": 3})
    candle_count_before = await _count_candles(oids[0]) + await _count_candles(oids[1])

    # admin settle（ResolveRequest 要求 payout 字段）
    resp = await client.post(
        f"/api/v1/market/{mid}/resolve",
        headers=admin_headers,
        json={"winning_outcome_id": oids[0], "payout": 1},
    )
    assert resp.status_code == 200, resp.text

    candle_count_after = await _count_candles(oids[0]) + await _count_candles(oids[1])
    assert candle_count_after == candle_count_before, "settle 不应写 candle"


@pytest.mark.asyncio
async def test_halt_blocks_trade_and_no_candle(client):
    """HALT 期间 buy 被拒 → candle 表无新行。"""
    mid, oids = await _seed_market(n_outcomes=2)
    _, headers = await _seed_user(cash=Decimal("1000"))
    admin_headers = await _seed_admin()

    # 先 buy 一笔
    await client.post("/api/v1/market/buy", headers=headers,
                      json={"outcome_id": oids[0], "shares": 1})
    candle_count_before = await _count_candles(oids[0])

    # HALT
    resp = await client.post(f"/api/v1/market/{mid}/close", headers=admin_headers)
    assert resp.status_code == 200

    # 再 buy → 拒绝
    resp = await client.post("/api/v1/market/buy", headers=headers,
                             json={"outcome_id": oids[0], "shares": 1})
    assert resp.status_code == 400

    candle_count_after = await _count_candles(oids[0])
    assert candle_count_after == candle_count_before
