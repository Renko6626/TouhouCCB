"""Short cash can repay gold debt; foreign debt still blocks consumption."""
from datetime import datetime, timezone
from decimal import Decimal
import uuid

import pytest
from sqlalchemy import select
from tests.conftest import publish_seeded_market_records

from app.core.database import async_session_maker
from app.models.base import SiteConfig, User
from app.models.fx import FxPair, FxShortPosition
from app.models.redemption import BatchStatus, RedemptionBatch, RedemptionCode, RedemptionPartner
from app.services import admin_user_service as admin, danmuku, loan_service, redemption
from app.services.credit import flags
from app.services.credit.flags import CreditFlags

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures('published_markets')]
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
        for _ in range(2):
            pair = FxPair(currency_code=uuid.uuid4().hex[:16], currency_name="test",
                          status=pair_status,
                          gold_reserve=D("10000"), foreign_reserve=D("10000"))
            s.add(pair)
            await s.flush()
            s.add(FxShortPosition(user_id=user.id, pair_id=pair.id,
                                 principal_foreign=D("10"), interest_last_accrued_at=datetime.now(timezone.utc)))
        await s.commit()
        return user.id, operator.id


async def state(uid):
    async with async_session_maker() as s:
        user = await s.get(User, uid)
        return user.cash, user.debt


async def test_gold_repayment_uses_total_cash():
    uid, _ = await seed(debt="70")
    async with async_session_maker() as s:
        _, repaid = await loan_service.decrease_debt(
            s, uid, D("100"), consume_cash=True, daily_rate=D("0"), source="repay")
        await s.commit()
    assert repaid == D("70")
    assert await state(uid) == (D("30"), D("0"))


@pytest.mark.parametrize("unified", [False, True])
async def test_amnesty_resets_cash_with_short_debt_when_margin_sufficient(unified):
    uid, operator = await seed(debt="10")
    if unified:
        flags.set_flags(CreditFlags( credit_leverage=D("4"), credit_maintenance_ratio=D("0.1")))
    async with async_session_maker() as s:
        s.add(SiteConfig(key="loan_daily_rate", value="0", value_type="decimal"))
        await s.commit()
        result = await admin.amnesty(s, f=admin.UserFilter(user_id_min=uid, user_id_max=uid),
                                    reset_cash_to=D("70"), forgive_debt=True,
                                    reason="cash purpose", admin_id=operator, dry_run=False)
    assert result["updated_count"] == 1
    assert await state(uid) == (D("70"), D("0"))


@pytest.mark.parametrize("product", ["redemption", "danmuku"])
async def test_foreign_only_debt_blocks_consumption_even_with_cash(product):
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
    assert await state(uid) == (D("100"), D("0"))


@pytest.mark.parametrize("buy_fee, succeeds", [("0", True), ("0.5", False)])
async def test_open_proceeds_buy_another_pair_subject_to_initial_margin(buy_fee, succeeds):
    from fastapi import HTTPException
    from app.models.fx import FxWallet, FxTreasury
    from app.services.fx import trading
    from tests.test_fx_short_trading import _seed, _open

    flags.set_flags(CreditFlags(credit_leverage=D("4"), credit_maintenance_ratio=D("0.1")))
    uid, short_pid = await _seed(cash="50")
    await publish_seeded_market_records()
    await _open(uid, short_pid, "100")
    async with async_session_maker() as s:
        pair = FxPair(currency_code=uuid.uuid4().hex[:16], currency_name="Other",
                      status="trading", gold_reserve=D("10000"),
                      foreign_reserve=D("10000"), buy_fee_rate=D(buy_fee))
        s.add(pair)
        await s.flush()
        s.add(FxTreasury(pair_id=pair.id, gold_balance=D("10000"), foreign_balance=D("10000")))
        await s.commit()
        pid = pair.id
    await publish_seeded_market_records()
    before = await state(uid)
    assert before[0] > D("100")
    async with async_session_maker() as s:
        if succeeds:
            result = await trading.execute_trade(s, uid, pid, "buy", D("100"), D("0"), uuid.uuid4().hex)
            await s.commit()
            assert result.output_amount > 0
        else:
            with pytest.raises(HTTPException) as exc:
                await trading.execute_trade(s, uid, pid, "buy", D("100"), D("0"), uuid.uuid4().hex)
            assert "margin" in str(exc.value.detail)
            await s.rollback()
    after = await state(uid)
    assert after == ((before[0] - D("100"), before[1]) if succeeds else before)
    async with async_session_maker() as s:
        wallet = (await s.execute(select(FxWallet).where(
            FxWallet.user_id == uid, FxWallet.pair_id == pid))).scalar_one_or_none()
        assert (wallet is not None and wallet.foreign_amount > 0) if succeeds else wallet is None
