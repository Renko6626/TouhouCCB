from decimal import Decimal
import uuid

import pytest
from sqlalchemy import func, select

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.audit import AuditEvent
from app.models.base import SiteConfig, User
from app.models.redemption import DanmukuExchange, RedemptionBatch, RedemptionCode, RedemptionPartner, RedemptionTransaction
from app.services import danmuku, redemption


async def seed_account(debt=Decimal("0"), cash=Decimal("500")):
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        user = User(username=f"buyer_{suffix}", casdoor_id=suffix, cash=cash, debt=debt)
        partner = RedemptionPartner(name="活动兑换台")
        db.add_all([user, partner])
        await db.flush()
        batch = RedemptionBatch(partner_id=partner.id, name="纪念品", unit_price=Decimal("100"), status="active")
        db.add(batch)
        await db.flush()
        code = RedemptionCode(batch_id=batch.id, code_string=f"guard_{suffix}")
        db.add(code)
        db.add_all([
            SiteConfig(key="loan_enabled", value="true", value_type="bool"),
            SiteConfig(key="loan_leverage_k", value="1", value_type="decimal"),
            SiteConfig(key="loan_daily_rate", value="0", value_type="decimal"),
        ])
        await db.commit()
        return user.id, batch.id, code.id, {"Authorization": f"Bearer {create_access_token(user.id)}"}


def purchase_request(kind, batch_id):
    if kind == "coupon":
        return "/api/v1/redemption/purchase", {"batch_id": batch_id}
    return "/api/v1/danmuku/exchange", {"qq_user_id": "123456789", "room_id": "弹幕群", "yuan": "100", "huo": "0"}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["coupon", "danmuku"])
@pytest.mark.parametrize("debt", [Decimal("100"), Decimal("0.000001")])
async def test_outstanding_debt_blocks_even_with_sufficient_cash_without_delivery(client, kind, debt):
    uid, bid, cid, headers = await seed_account(debt=debt, cash=Decimal("10000"))
    url, body = purchase_request(kind, bid)
    response = await client.post(url, json=body, headers=headers)
    assert response.status_code == 403, response.text
    assert "还清" in response.json()["detail"]
    async with async_session_maker() as db:
        user = await db.get(User, uid)
        assert user.cash == Decimal("10000") and user.debt == debt
        code = await db.get(RedemptionCode, cid)
        assert code.status == "available" and code.bought_by_user_id is None
        for model in (RedemptionTransaction, DanmukuExchange, AuditEvent):
            assert (await db.execute(select(func.count()).select_from(model))).scalar_one() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["coupon", "danmuku"])
async def test_actual_borrow_blocks_purchase_and_full_repayment_restores_purchase(client, kind):
    uid, bid, cid, headers = await seed_account()
    borrowed = await client.post("/api/v1/loan/borrow", json={"amount": "100"}, headers=headers)
    assert borrowed.status_code == 200, borrowed.text
    url, body = purchase_request(kind, bid)
    blocked = await client.post(url, json=body, headers=headers)
    assert blocked.status_code == 403
    repaid = await client.post("/api/v1/loan/repay-all", headers=headers)
    assert repaid.status_code == 200, repaid.text
    assert Decimal(str(repaid.json()["debt"])) == 0
    bought = await client.post(url, json=body, headers=headers)
    assert bought.status_code == 200, bought.text
    assert bought.json()["code_string"]
    async with async_session_maker() as db:
        user = await db.get(User, uid)
        assert user.cash == Decimal("400") and user.debt == 0
        if kind == "coupon":
            code = await db.get(RedemptionCode, cid)
            assert code.status == "sold" and code.bought_by_user_id == uid
        else:
            assert (await db.execute(select(func.count()).select_from(DanmukuExchange))).scalar_one() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["coupon", "danmuku"])
async def test_locked_latest_user_overrides_stale_zero_debt_session(kind):
    uid, bid, _, _ = await seed_account()
    async with async_session_maker() as stale:
        cached = await stale.get(User, uid)
        assert cached.debt == 0
        async with async_session_maker() as other:
            user = await other.get(User, uid)
            user.debt = Decimal("1")
            await other.commit()
        if kind == "coupon":
            with pytest.raises(redemption.PurchaseError) as error:
                await redemption.purchase_code(stale, user_id=uid, batch_id=bid)
        else:
            with pytest.raises(danmuku.ExchangeError) as error:
                await danmuku.exchange(stale, user_id=uid, qq_user_id="123456789", room_id="弹幕群",
                                       yuan=Decimal("100"), huo=Decimal("0"))
        assert error.value.code == "OUTSTANDING_DEBT"
        await stale.rollback()
