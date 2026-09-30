"""Restricted short proceeds cannot finance other products or gold debt.

These persisted scenarios catch cash-purpose violations, independently of the
later foreign-margin integration. No simulated quote or source-text assertions.
"""
from datetime import datetime, timezone
from decimal import Decimal
import uuid

import pytest
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.base import SiteConfig, User
from app.models.fx import FxPair, FxShortPosition
from app.models.redemption import BatchStatus, RedemptionBatch, RedemptionCode, RedemptionPartner
from app.services import admin_user_service as admin, danmuku, loan_service, redemption
from app.services.credit import flags
from app.services.credit.flags import CreditFlags

pytestmark = pytest.mark.asyncio
D = Decimal


@pytest.fixture(autouse=True)
def clean_flags():
    flags.clear_flags()
    yield
    flags.clear_flags()
    flags.set_new_risk_frozen(None)


async def seed(*, debt="0", frozen=False, pair_status="trading"):
    # A real, coverable short lives on a trading pair; draft pairs keep the
    # obligation but must block increased risk (spec §9), so the healthy
    # fixture cannot rely on the model's draft default.
    async with async_session_maker() as s:
        user = User(username=uuid.uuid4().hex, email=f"{uuid.uuid4().hex}@test.com",
                    casdoor_id=uuid.uuid4().hex, cash=D("100"), debt=D(debt),
                    credit_frozen=frozen)
        operator = User(username=uuid.uuid4().hex, email=f"{uuid.uuid4().hex}@test.com",
                        casdoor_id=uuid.uuid4().hex, is_superuser=True)
        s.add_all([user, operator])
        await s.flush()
        for lock in ("50", "30"):
            pair = FxPair(currency_code=uuid.uuid4().hex[:16], currency_name="test",
                          status=pair_status,
                          gold_reserve=D("10000"), foreign_reserve=D("10000"))
            s.add(pair)
            await s.flush()
            s.add(FxShortPosition(user_id=user.id, pair_id=pair.id,
                                 principal_foreign=D("10"), restricted_gold=D(lock),
                                 interest_last_accrued_at=datetime.now(timezone.utc)))
        await s.commit()
        return user.id, operator.id


async def state(uid):
    async with async_session_maker() as s:
        user = await s.get(User, uid)
        locks = (await s.execute(select(FxShortPosition.restricted_gold)
                                .where(FxShortPosition.user_id == uid))).scalars().all()
        return user.cash, user.debt, sum(locks, D("0"))


async def test_gold_repayment_caps_at_unrestricted_cash():
    uid, _ = await seed(debt="70")
    async with async_session_maker() as s:
        _, repaid = await loan_service.decrease_debt(
            s, uid, D("30"), consume_cash=True, daily_rate=D("0"), source="repay")
        await s.commit()
    assert repaid == D("20")
    assert await state(uid) == (D("80"), D("50"), D("80"))


@pytest.mark.parametrize("unified", [False, True])
async def test_admin_cash_debit_rejects_locked_proceeds_but_allows_exact_available(unified):
    uid, operator = await seed()
    if unified:
        flags.set_flags(CreditFlags(unified_credit_enabled=True, credit_leverage=D("4"), credit_maintenance_ratio=D("0.1")))
    async with async_session_maker() as s:
        with pytest.raises(admin.AdminUserError):
            await admin.adjust_cash(s, target_id=uid, amount=D("-30"),
                                    reason="cash purpose", admin_id=operator)
        await s.rollback()
    assert await state(uid) == (D("100"), D("0"), D("80"))
    async with async_session_maker() as s:
        await admin.adjust_cash(s, target_id=uid, amount=D("-20"),
                                reason="cash purpose", admin_id=operator)
    assert await state(uid) == (D("80"), D("0"), D("80"))


async def test_draft_short_debt_blocks_increased_risk_debit():
    """Draft pair keeps the obligation but cannot be covered: increased risk denied.

    Explicit drafting regression guard for spec §9: the healthy fixture above is
    ``trading``; here a draft short with the same free cash must be rejected
    instead of the production path quietly allowing it.
    """
    uid, operator = await seed(pair_status="draft")
    flags.set_flags(CreditFlags(unified_credit_enabled=True, credit_leverage=D("4"),
                                credit_maintenance_ratio=D("0.1")))
    async with async_session_maker() as s:
        with pytest.raises(admin.AdminUserError) as exc:
            await admin.adjust_cash(s, target_id=uid, amount=D("-20"),
                                    reason="draft short", admin_id=operator)
        await s.rollback()
    assert exc.value.status == 409
    assert "draft" in exc.value.detail
    assert await state(uid) == (D("100"), D("0"), D("80"))


@pytest.mark.parametrize("unified", [False, True])
async def test_amnesty_cannot_reset_below_other_short_locks_even_when_forgiving_gold(unified):
    uid, operator = await seed(debt="10", frozen=True)
    if unified:
        flags.set_flags(CreditFlags(unified_credit_enabled=True, credit_leverage=D("4"), credit_maintenance_ratio=D("0.1")))
    async with async_session_maker() as s:
        s.add(SiteConfig(key="loan_daily_rate", value="0", value_type="decimal"))
        await s.commit()
        result = await admin.amnesty(s, f=admin.UserFilter(user_id_min=uid, user_id_max=uid),
                                    reset_cash_to=D("70"), forgive_debt=True,
                                    reason="cash purpose", admin_id=operator, dry_run=False)
    assert result["updated_count"] == 0
    assert await state(uid) == (D("100"), D("10"), D("80"))


@pytest.mark.parametrize("product", ["redemption", "danmuku"])
async def test_foreign_only_debt_blocks_consumption_even_with_unrestricted_cash(product):
    uid, _ = await seed()
    async with async_session_maker() as s:
        if product == "redemption":
            partner = RedemptionPartner(name="test")
            s.add(partner)
            await s.flush()
            batch = RedemptionBatch(partner_id=partner.id, name="test", unit_price=D("10"),
                                    status=BatchStatus.ACTIVE)
            s.add(batch)
            await s.flush()
            s.add(RedemptionCode(batch_id=batch.id, code_string=uuid.uuid4().hex))
            await s.commit()
            with pytest.raises(redemption.PurchaseError) as exc:
                await redemption.purchase_code(s, user_id=uid, batch_id=batch.id)
        else:
            with pytest.raises(danmuku.ExchangeError) as exc:
                await danmuku.exchange(s, user_id=uid, qq_user_id="1", room_id="test",
                                      yuan=D("10"), huo=D("0"))
        assert exc.value.code == "OUTSTANDING_DEBT"
        await s.rollback()
    assert await state(uid) == (D("100"), D("0"), D("80"))
