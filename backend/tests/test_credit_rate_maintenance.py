"""Rate switches cannot retroactively reprice debt or lose dust clock boundaries."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from uuid import uuid4

import pytest
from sqlalchemy import select
from app.core.database import async_session_maker
from app.models.base import User, SiteConfig
from app.models.fx import FxPair, FxShortPosition
from app.models.audit import AuditEvent
from app.services.loan_service import pending_debt
from app.services.fx.shorts import pending_short_debt
from app.services.credit.rate_maintenance import _change_rate_in_session, RateMaintenanceError

pytestmark = pytest.mark.asyncio
T = datetime(2026, 10, 1, tzinfo=timezone.utc)

async def seed(amount="100", invalid=None):
    async with async_session_maker() as db:
        u = User(username=uuid4().hex, cash=D("123"), debt=D(amount),
                 debt_last_accrued_at=T-timedelta(days=1))
        pair = FxPair(currency_code=uuid4().hex[:8], currency_name="maintenance")
        db.add_all([u, pair, SiteConfig(key="loan_daily_rate", value="0.1", value_type="decimal")])
        await db.flush()
        p = FxShortPosition(user_id=u.id, pair_id=pair.id, principal_foreign=D(amount),
                            interest_last_accrued_at=T-timedelta(days=1), restricted_gold=D("2"), proceeds_basis_gold=D("3"))
        db.add(p)
        if invalid:
            db.add(User(username=uuid4().hex,
                        debt=D("1") if invalid == "clock" else D("9999999999"),
                        debt_last_accrued_at=None if invalid == "clock" else T-timedelta(days=1)))
        await db.commit()
        return u.id, p.id

async def switch(db):
    async with db.begin():
        await _change_rate_in_session(db, new_rate=D("0.2"), operator_user_id=None,
                                      now=T, assert_offline_owner=lambda: None)

async def test_old_rate_settlement_then_new_rate():
    uid, pid = await seed()
    async with async_session_maker() as db:
        await switch(db)
        u, p = await db.get(User, uid), await db.get(FxShortPosition, pid)
        assert u.debt == D("110") and p.interest_foreign == D("10")
        assert u.cash == D("123") and p.principal_foreign == D("100")
        assert p.restricted_gold == D("2") and p.proceeds_basis_gold == D("3")
        assert u.economic_version == 1
        assert pending_debt(u, D("0.2"), T+timedelta(days=1)) == D("132")
        assert pending_short_debt(p, D("0.2"), T+timedelta(days=1)) == D("132")
        events = (await db.execute(select(AuditEvent).order_by(AuditEvent.id))).scalars().all()
        assert [e.event_type for e in events] == ["interest_accrual", "interest_accrual", "config_set"]
        assert events[-1].payload["old"] == "0.1" and events[-1].payload["new"] == "0.2"

async def test_dust_clocks_advance_and_are_audited():
    uid, pid = await seed("0.000001")
    async with async_session_maker() as db:
        await switch(db)
        u, p = await db.get(User, uid), await db.get(FxShortPosition, pid)
        assert u.debt == D("0.000001") and p.interest_foreign == 0
        assert u.debt_last_accrued_at.replace(tzinfo=timezone.utc) == T
        assert p.interest_last_accrued_at.replace(tzinfo=timezone.utc) == T
        events = (await db.execute(select(AuditEvent).where(AuditEvent.event_type == "interest_accrual"))).scalars().all()
        assert len(events) == 2 and all(e.payload["interest"] == "0.000000" or D(e.payload["interest"]) == 0 for e in events)
        assert events[1].payload["interest_foreign_delta"] == "0.000000"
        assert events[1].payload["source"] == "rate_maintenance"

@pytest.mark.parametrize("invalid", ["clock", "overflow"])
async def test_invalid_debt_rolls_back_everything(invalid):
    uid, pid = await seed(invalid=invalid)
    async with async_session_maker() as db:
        with pytest.raises(RateMaintenanceError):
            await switch(db)
    async with async_session_maker() as db:
        assert (await db.get(User, uid)).debt == D("100")
        assert (await db.get(FxShortPosition, pid)).interest_foreign == 0
        assert (await db.execute(select(SiteConfig).where(SiteConfig.key == "loan_daily_rate"))).scalar_one().value == "0.1"
        assert not (await db.execute(select(AuditEvent))).scalars().all()
