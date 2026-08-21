"""spec § 7.5：_resync_recent_candles 必须确定性——同一批 Transaction 重放
两次得到逐字段相同的 OutcomeCandle 行集。

updated_at 豁免：upsert_candles UPDATE 分支写 func.now()，非确定；但它不进
/history/ 的列式编码，不违反 immutable 承诺。
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlmodel import SQLModel, select

from app.core.database import engine, async_session_maker
from app.main import _resync_recent_candles
from app.models.base import (
    Market, MarketStatus, Outcome, OutcomeCandle, Transaction, TransactionType, User,
)


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
        await conn.run_sync(SQLModel.metadata.create_all)
    yield


async def _seed_market_with_trades() -> list[int]:
    now = datetime.now(timezone.utc)
    async with async_session_maker() as s:
        m = Market(title="t", description="", liquidity_b=100.0, status=MarketStatus.TRADING)
        s.add(m); await s.flush()
        o1 = Outcome(market_id=m.id, label="a", total_shares=Decimal("30"))
        o2 = Outcome(market_id=m.id, label="b", total_shares=Decimal("0"))
        s.add(o1); s.add(o2); await s.flush()
        u = User(username="alice", email="a@x.com", hashed_password="x",
                 cash=Decimal("1000"), is_active=True)
        s.add(u); await s.flush()
        for i, (shares, post) in enumerate([("10", [0.52, 0.48]), ("20", [0.56, 0.44])]):
            s.add(Transaction(
                user_id=u.id, outcome_id=o1.id, type=TransactionType.BUY,
                shares=Decimal(shares), cost=Decimal("5"), price=Decimal("0.5"),
                pre_market_price=Decimal("0.5"), post_market_price=Decimal(str(post[0])),
                gross=Decimal("5"), fee=Decimal("0"),
                market_prices_post=post,
                timestamp=now - timedelta(minutes=5) + timedelta(seconds=i * 7),
            ))
        await s.commit()
        return [int(o1.id), int(o2.id)]


def _fingerprint(rows: list[OutcomeCandle]) -> list[tuple]:
    return sorted(
        (r.outcome_id, r.interval, r.bucket_start.replace(tzinfo=None),
         r.open_price, r.high_price, r.low_price, r.close_price,
         r.volume_shares, r.n_trades)
        for r in rows
    )


@pytest.mark.asyncio
async def test_resync_twice_yields_identical_rows():
    oids = await _seed_market_with_trades()
    await _resync_recent_candles(window_hours=1)
    async with async_session_maker() as s:
        first = _fingerprint((await s.execute(
            select(OutcomeCandle).where(OutcomeCandle.outcome_id.in_(oids))
        )).scalars().all())
    assert first, "resync 应产出 candle 行"
    await _resync_recent_candles(window_hours=1)   # 第二次：DELETE + 重放
    async with async_session_maker() as s:
        second = _fingerprint((await s.execute(
            select(OutcomeCandle).where(OutcomeCandle.outcome_id.in_(oids))
        )).scalars().all())
    assert first == second
