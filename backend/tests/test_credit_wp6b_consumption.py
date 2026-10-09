"""WP6b：兑换码购买 / 弹幕兑换的现金消费准入（flag=unified_credit_enabled）。

覆盖：扣款前 E/冻结检查、版本自增、冻结期拒绝、legacy 语义（有债禁止兑换）
保留，以及 flag OFF 的旧行为逐字段不变。
"""
import os
import sys
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.database import async_session_maker
from app.models.base import User
from app.models.redemption import (
    BatchStatus, CodeStatus, RedemptionBatch, RedemptionCode, RedemptionPartner,
)
from app.services import danmuku as danmuku_svc
from app.services import redemption as redemption_svc
from app.services.credit import flags as credit_flags
from app.services.credit.flags import CreditFlags
from app.services.credit.ownership import WriteOwnership
from app.services.credit.version import economic_version_of

pytestmark = pytest.mark.asyncio

ZERO = Decimal("0")
SQLITE_URL = "sqlite+aiosqlite:////dev/shm/credit-wp6b.db"
UNIFIED = CreditFlags(
    credit_leverage=Decimal("4"),
    credit_maintenance_ratio=Decimal("0.1"),
)


@pytest.fixture(autouse=True)
def _clean_flags():
    credit_flags.clear_flags()
    yield
    credit_flags.clear_flags()
    credit_flags.set_new_risk_frozen(None)


@pytest_asyncio.fixture
async def writes_enabled(monkeypatch):
    own = WriteOwnership(url=SQLITE_URL)
    await own.acquire()
    monkeypatch.setattr(redemption_svc, "OWNERSHIP", own)
    monkeypatch.setattr(danmuku_svc, "OWNERSHIP", own)
    yield own
    await own.release()


async def _seed_user(*, cash: Decimal, debt: Decimal = ZERO, frozen: bool = False) -> int:
    async with async_session_maker() as s:
        u = User(
            username=f"u_{uuid.uuid4().hex[:8]}",
            email=f"{uuid.uuid4().hex[:8]}@t.com",
            casdoor_id=uuid.uuid4().hex,
            cash=cash,
            debt=debt,
            credit_frozen=frozen,
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return int(u.id)


async def _seed_batch(unit_price: Decimal) -> int:
    async with async_session_maker() as s:
        p = RedemptionPartner(name="P")
        s.add(p)
        await s.commit()
        await s.refresh(p)
        b = RedemptionBatch(
            partner_id=p.id, name="B", unit_price=unit_price, status=BatchStatus.ACTIVE,
        )
        s.add(b)
        await s.commit()
        await s.refresh(b)
        s.add(RedemptionCode(batch_id=b.id, code_string=f"C{uuid.uuid4().hex[:8]}"))
        await s.commit()
        return int(b.id)


async def _state(uid: int) -> tuple[Decimal, int]:
    async with async_session_maker() as s:
        u = (await s.execute(select(User).where(User.id == uid))).scalars().one()
        return Decimal(u.cash), economic_version_of(u)


async def _available_codes(batch_id: int) -> int:
    async with async_session_maker() as s:
        rows = (await s.execute(
            select(RedemptionCode).where(
                RedemptionCode.batch_id == batch_id,
                RedemptionCode.status == CodeStatus.AVAILABLE,
            )
        )).scalars().all()
        return len(rows)


# ────────────────────────── redemption ──────────────────────────


async def test_unified_purchase_deducts_and_bumps_version(writes_enabled):
    uid = await _seed_user(cash=Decimal("100"))
    batch_id = await _seed_batch(Decimal("10"))
    credit_flags.set_flags(UNIFIED)
    async with async_session_maker() as s:
        result = await redemption_svc.purchase_code(s, user_id=uid, batch_id=batch_id)
        await s.commit()
    assert result.cash_after == Decimal("90.000000")
    cash, version = await _state(uid)
    assert cash == Decimal("90.000000") and version == 1
    assert await _available_codes(batch_id) == 0


async def test_unified_purchase_gate_covers_commit(writes_enabled, monkeypatch):
    from contextlib import asynccontextmanager
    uid = await _seed_user(cash=Decimal("100"))
    batch_id = await _seed_batch(Decimal("10"))
    credit_flags.set_flags(UNIFIED)
    original = redemption_svc.GATES.hold
    observed = []
    async with async_session_maker() as s:
        @asynccontextmanager
        async def hold(**kwargs):
            assert not s.in_transaction(), "read transaction must close before gate wait"
            async with original(**kwargs):
                yield
                assert not s.in_transaction(), "mutation must commit before gate release"
                observed.append(await _state(uid))
        monkeypatch.setattr(redemption_svc.GATES, "hold", hold)
        await redemption_svc.purchase_code(s, user_id=uid, batch_id=batch_id)
    assert observed == [(Decimal("90.000000"), 1)]


async def _seed_operator_frozen() -> None:
    """运营停增险是 site_config 热读（refresh_new_risk_frozen 每次检查都会覆盖进程内值）。"""
    from app.models.base import SiteConfig
    async with async_session_maker() as s:
        s.add(SiteConfig(key="credit_new_risk_frozen", value="true", value_type="bool"))
        await s.commit()


async def test_unified_purchase_blocked_when_operator_frozen(writes_enabled):
    uid = await _seed_user(cash=Decimal("100"))
    batch_id = await _seed_batch(Decimal("10"))
    await _seed_operator_frozen()
    credit_flags.set_flags(UNIFIED)
    async with async_session_maker() as s:
        with pytest.raises(HTTPException) as exc:
            await redemption_svc.purchase_code(s, user_id=uid, batch_id=batch_id)
        await s.rollback()
    assert exc.value.status_code == 409
    cash, version = await _state(uid)
    assert cash == Decimal("100.000000") and version == 0
    assert await _available_codes(batch_id) == 1


async def test_unified_purchase_blocked_when_user_credit_frozen(writes_enabled):
    uid = await _seed_user(cash=Decimal("100"), frozen=True)
    batch_id = await _seed_batch(Decimal("10"))
    credit_flags.set_flags(UNIFIED)
    async with async_session_maker() as s:
        with pytest.raises(HTTPException) as exc:
            await redemption_svc.purchase_code(s, user_id=uid, batch_id=batch_id)
        await s.rollback()
    assert exc.value.status_code == 409
    cash, version = await _state(uid)
    assert cash == Decimal("100.000000") and version == 0


async def test_unified_purchase_keeps_outstanding_debt_guard(writes_enabled):
    uid = await _seed_user(cash=Decimal("100"), debt=Decimal("5"))
    batch_id = await _seed_batch(Decimal("10"))
    credit_flags.set_flags(UNIFIED)
    async with async_session_maker() as s:
        with pytest.raises(redemption_svc.PurchaseError) as exc:
            await redemption_svc.purchase_code(s, user_id=uid, batch_id=batch_id)
        await s.rollback()
    assert exc.value.code == "OUTSTANDING_DEBT"
    cash, version = await _state(uid)
    assert cash == Decimal("100.000000") and version == 0




# ────────────────────────── danmuku ──────────────────────────


async def test_unified_danmuku_deducts_and_bumps_version(writes_enabled):
    uid = await _seed_user(cash=Decimal("100"))
    credit_flags.set_flags(UNIFIED)
    async with async_session_maker() as s:
        result = await danmuku_svc.exchange(
            s, user_id=uid, qq_user_id="123", room_id="room",
            yuan=Decimal("10"), huo=Decimal("5"),
        )
        await s.commit()
    assert result.amount == Decimal("15.000000")
    cash, version = await _state(uid)
    assert cash == Decimal("85.000000") and version == 1


async def test_unified_danmuku_blocked_when_frozen(writes_enabled):
    uid = await _seed_user(cash=Decimal("100"), frozen=True)
    credit_flags.set_flags(UNIFIED)
    async with async_session_maker() as s:
        with pytest.raises(HTTPException) as exc:
            await danmuku_svc.exchange(
                s, user_id=uid, qq_user_id="123", room_id="room",
                yuan=Decimal("10"), huo=Decimal("0"),
            )
        await s.rollback()
    assert exc.value.status_code == 409
    cash, version = await _state(uid)
    assert cash == Decimal("100.000000") and version == 0




async def test_unified_danmuku_keeps_outstanding_debt_guard(writes_enabled):
    uid = await _seed_user(cash=Decimal("100"), debt=Decimal("5"))
    credit_flags.set_flags(UNIFIED)
    async with async_session_maker() as s:
        with pytest.raises(danmuku_svc.ExchangeError) as exc:
            await danmuku_svc.exchange(
                s, user_id=uid, qq_user_id="123", room_id="room",
                yuan=Decimal("10"), huo=Decimal("0"),
            )
        await s.rollback()
    assert exc.value.code == "OUTSTANDING_DEBT"
    cash, version = await _state(uid)
    assert cash == Decimal("100.000000") and version == 0
