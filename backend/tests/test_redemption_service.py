import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import asyncio
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlmodel import SQLModel

from app.core.database import engine, async_session_maker
from app.models.base import User
from app.models.redemption import (
    RedemptionPartner, RedemptionBatch, RedemptionCode,
    BatchStatus,
)
from app.services.redemption import (
    parse_csv_codes,
    purchase_code,
    PurchaseError,
)


@pytest_asyncio.fixture
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
        await conn.run_sync(SQLModel.metadata.create_all)
    yield


async def _seed_user(cash: Decimal) -> int:
    async with async_session_maker() as s:
        u = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            email=f"{uuid.uuid4().hex[:6]}@t.com",
            casdoor_id=uuid.uuid4().hex,
            cash=cash,
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return u.id


async def _seed_batch(unit_price: Decimal, code_strings, status=BatchStatus.ACTIVE) -> int:
    async with async_session_maker() as s:
        p = RedemptionPartner(name="P")
        s.add(p)
        await s.commit()
        await s.refresh(p)
        b = RedemptionBatch(
            partner_id=p.id, name="B", unit_price=unit_price, status=status,
        )
        s.add(b)
        await s.commit()
        await s.refresh(b)
        for cs in code_strings:
            s.add(RedemptionCode(batch_id=b.id, code_string=cs))
        await s.commit()
        return b.id


def test_parse_csv_simple_lines():
    text = "ABC123\nDEF456\nGHI789\n"
    valid, invalid = parse_csv_codes(text)
    assert valid == ["ABC123", "DEF456", "GHI789"]
    assert invalid == []


def test_parse_csv_skips_header_named_code():
    text = "code\nABC\nDEF\n"
    valid, invalid = parse_csv_codes(text)
    assert valid == ["ABC", "DEF"]


def test_parse_csv_trims_whitespace_and_empty_lines():
    text = "  ABC  \n\n   \n DEF\n"
    valid, invalid = parse_csv_codes(text)
    assert valid == ["ABC", "DEF"]


def test_parse_csv_marks_too_long_as_invalid():
    long = "X" * 129
    text = f"OK\n{long}\n"
    valid, invalid = parse_csv_codes(text)
    assert valid == ["OK"]
    assert invalid == [long]


def test_parse_csv_dedupes_within_input():
    text = "ABC\nABC\nDEF\n"
    valid, invalid = parse_csv_codes(text)
    assert valid == ["ABC", "DEF"]


@pytest.mark.asyncio
async def test_purchase_happy_path(setup_db):
    uid = await _seed_user(Decimal("100"))
    bid = await _seed_batch(Decimal("30"), ["A1", "A2"])
    async with async_session_maker() as s:
        result = await purchase_code(s, user_id=uid, batch_id=bid)
        await s.commit()
    assert result.code_string in {"A1", "A2"}
    assert result.paid_amount == Decimal("30")
    async with async_session_maker() as s:
        u = await s.get(User, uid)
        assert u.cash == Decimal("70")


@pytest.mark.asyncio
async def test_purchase_insufficient_cash(setup_db):
    uid = await _seed_user(Decimal("10"))
    bid = await _seed_batch(Decimal("30"), ["A1"])
    async with async_session_maker() as s:
        with pytest.raises(PurchaseError) as e:
            await purchase_code(s, user_id=uid, batch_id=bid)
        assert e.value.code == "INSUFFICIENT_CASH"


@pytest.mark.asyncio
async def test_purchase_sold_out(setup_db):
    uid = await _seed_user(Decimal("100"))
    bid = await _seed_batch(Decimal("30"), [])
    async with async_session_maker() as s:
        with pytest.raises(PurchaseError) as e:
            await purchase_code(s, user_id=uid, batch_id=bid)
        assert e.value.code == "SOLD_OUT"


@pytest.mark.asyncio
async def test_purchase_batch_not_active(setup_db):
    uid = await _seed_user(Decimal("100"))
    bid = await _seed_batch(Decimal("30"), ["A1"], status=BatchStatus.DRAFT)
    async with async_session_maker() as s:
        with pytest.raises(PurchaseError) as e:
            await purchase_code(s, user_id=uid, batch_id=bid)
        assert e.value.code == "BATCH_NOT_ACTIVE"


@pytest.mark.skipif(
    "sqlite" in str(engine.url),
    reason="SQLite 不支持 FOR UPDATE SKIP LOCKED 真并发，仅 Postgres 上验证此场景",
)
@pytest.mark.asyncio
async def test_purchase_concurrent_only_one_wins(setup_db):
    """同时两人抢最后一个码，仅一个成功，另一个 SOLD_OUT（PG 语义）。"""
    uid1 = await _seed_user(Decimal("100"))
    uid2 = await _seed_user(Decimal("100"))
    bid = await _seed_batch(Decimal("30"), ["LAST"])

    async def _try(uid):
        async with async_session_maker() as s:
            try:
                r = await purchase_code(s, user_id=uid, batch_id=bid)
                await s.commit()
                return ("ok", r.code_string)
            except PurchaseError as e:
                return ("err", e.code)

    res = await asyncio.gather(_try(uid1), _try(uid2))
    oks = [r for r in res if r[0] == "ok"]
    errs = [r for r in res if r[0] == "err"]
    assert len(oks) == 1
    assert len(errs) == 1
    assert errs[0][1] == "SOLD_OUT"
