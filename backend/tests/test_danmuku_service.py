import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from decimal import Decimal

import pytest

from app.core.database import async_session_maker
from app.models.base import User
from app.models.redemption import DanmukuExchange
from app.services.danmuku import exchange, generate_giftcode, ExchangeError


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


@pytest.mark.asyncio
async def test_generate_giftcode_deterministic_format(setup_db):
    code = generate_giftcode(yuan=10.0, huo=5.0, user_id="123456", room_id="弹幕群")
    # 格式：base64url(payload).hex(signature)
    assert "." in code
    payload_b64, sig_hex = code.split(".", 1)
    assert len(sig_hex) == 64  # sha256 hex
    assert payload_b64  # non-empty base64


@pytest.mark.asyncio
async def test_exchange_happy_path(setup_db):
    uid = await _seed_user(Decimal("100"))
    async with async_session_maker() as s:
        result = await exchange(
            s, user_id=uid, qq_user_id="123456", room_id="弹幕群",
            yuan=Decimal("10"), huo=Decimal("5"),
        )
        await s.commit()
        assert result.amount == Decimal("15.000000")
        assert result.cash_after == Decimal("85.000000")
        assert result.code_string and "." in result.code_string


@pytest.mark.asyncio
async def test_exchange_insufficient_cash(setup_db):
    uid = await _seed_user(Decimal("5"))
    async with async_session_maker() as s:
        with pytest.raises(ExchangeError) as exc:
            await exchange(
                s, user_id=uid, qq_user_id="123", room_id="弹幕群",
                yuan=Decimal("10"), huo=Decimal("0"),
            )
        assert exc.value.code == "INSUFFICIENT_CASH"


@pytest.mark.asyncio
async def test_exchange_invalid_zero_amount(setup_db):
    uid = await _seed_user(Decimal("100"))
    async with async_session_maker() as s:
        with pytest.raises(ExchangeError) as exc:
            await exchange(
                s, user_id=uid, qq_user_id="123", room_id="弹幕群",
                yuan=Decimal("0"), huo=Decimal("0"),
            )
        assert exc.value.code == "INVALID_AMOUNT"


@pytest.mark.asyncio
async def test_exchange_negative_amount(setup_db):
    uid = await _seed_user(Decimal("100"))
    async with async_session_maker() as s:
        with pytest.raises(ExchangeError) as exc:
            await exchange(
                s, user_id=uid, qq_user_id="123", room_id="弹幕群",
                yuan=Decimal("-1"), huo=Decimal("5"),
            )
        assert exc.value.code == "INVALID_AMOUNT"


@pytest.mark.asyncio
async def test_exchange_persists_record(setup_db):
    uid = await _seed_user(Decimal("100"))
    async with async_session_maker() as s:
        result = await exchange(
            s, user_id=uid, qq_user_id="456789", room_id="主播甲的房",
            yuan=Decimal("3"), huo=Decimal("2"),
        )
        await s.commit()
        # 重新查一次确认落库
        from sqlalchemy import select
        row = (await s.execute(
            select(DanmukuExchange).where(DanmukuExchange.id == result.id)
        )).scalar_one()
        assert row.user_id == uid
        assert row.qq_user_id == "456789"
        assert row.room_id == "主播甲的房"
        assert row.yuan == Decimal("3.000000")
        assert row.huo == Decimal("2.000000")
        assert row.amount == Decimal("5.000000")
        assert row.code_string == result.code_string
