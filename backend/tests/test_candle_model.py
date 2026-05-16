"""OutcomeCandle 模型字段、约束、PK 验证。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.core.database import async_session_maker
from app.models.base import Market, MarketStatus, Outcome, OutcomeCandle


async def _seed_outcome() -> int:
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        m = Market(title=f"m_{suffix}", status=MarketStatus.TRADING, liquidity_b=10.0)
        s.add(m)
        await s.flush()
        o = Outcome(market_id=m.id, label="opt_0", total_shares=Decimal("0"))
        s.add(o)
        await s.commit()
        await s.refresh(o)
        return o.id


@pytest.mark.asyncio
async def test_basic_insert_and_read(client):
    oid = await _seed_outcome()
    bucket = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    async with async_session_maker() as s:
        s.add(OutcomeCandle(
            outcome_id=oid, interval="1m", bucket_start=bucket,
            open_price=Decimal("0.5"), high_price=Decimal("0.6"),
            low_price=Decimal("0.4"), close_price=Decimal("0.55"),
            volume_shares=Decimal("10"), n_trades=3,
        ))
        await s.commit()
        row = (await s.execute(
            select(OutcomeCandle).where(OutcomeCandle.outcome_id == oid)
        )).scalars().first()
        assert row.close_price == Decimal("0.55000000")
        assert row.n_trades == 3


@pytest.mark.asyncio
async def test_unsupported_interval_rejected(client):
    oid = await _seed_outcome()
    bucket = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    async with async_session_maker() as s:
        s.add(OutcomeCandle(
            outcome_id=oid, interval="42s",  # 不在白名单
            bucket_start=bucket,
            open_price=Decimal("0.5"), high_price=Decimal("0.5"),
            low_price=Decimal("0.5"), close_price=Decimal("0.5"),
        ))
        with pytest.raises(IntegrityError):
            await s.commit()


@pytest.mark.asyncio
async def test_high_less_than_low_rejected(client):
    oid = await _seed_outcome()
    bucket = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    async with async_session_maker() as s:
        s.add(OutcomeCandle(
            outcome_id=oid, interval="10s", bucket_start=bucket,
            open_price=Decimal("0.5"),
            high_price=Decimal("0.3"),  # h < l 违反约束
            low_price=Decimal("0.5"),
            close_price=Decimal("0.5"),
        ))
        with pytest.raises(IntegrityError):
            await s.commit()


@pytest.mark.asyncio
async def test_negative_volume_rejected(client):
    oid = await _seed_outcome()
    bucket = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    async with async_session_maker() as s:
        s.add(OutcomeCandle(
            outcome_id=oid, interval="10s", bucket_start=bucket,
            open_price=Decimal("0.5"), high_price=Decimal("0.5"),
            low_price=Decimal("0.5"), close_price=Decimal("0.5"),
            volume_shares=Decimal("-1"),
        ))
        with pytest.raises(IntegrityError):
            await s.commit()
