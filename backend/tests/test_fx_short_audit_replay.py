"""Real ledger replay catches foreign debt/stock drift without double-booking gold."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
import pytest
from sqlalchemy import select
from app.core.database import async_session_maker
from app.models.fx import FxShortPosition, FxTreasury
from app.services import audit_replay, admin_user_service
from tests.test_fx_short_trading import _seed, _open, _cover, _credit_flags
pytestmark = pytest.mark.asyncio

async def test_short_replay_interest_trades_cutoff_and_tampering():
    uid, pid = await _seed(rate="0.01", buy_fee="0.01", sell_fee="0.01")
    await _open(uid, pid, "10")
    future = datetime.now(timezone.utc) + timedelta(days=1)
    with patch("app.services.fx.shorts.utcnow", return_value=future):
        await _open(uid, pid, "5")
        await _cover(uid, pid, q="3")
        async with async_session_maker() as db:
            events = await audit_replay.load_events(db)
            snap, errors = audit_replay.fold(events, check=True)
            assert errors == []
            assert await audit_replay.compare_with_live(db, snap) == []
            opening = next(e for e in events if e.payload.get("purpose") == "short_open")
            cutoff, errors = audit_replay.fold(await audit_replay.load_events(db, upto_id=opening.id), check=True)
            assert errors == []
            assert cutoff.fx_shorts[uid, pid].principal_foreign == Decimal("10")
            position = (await db.execute(select(FxShortPosition))).scalars().one()
            treasury = (await db.execute(select(FxTreasury))).scalars().one()
            position.restricted_gold += Decimal("1")
            treasury.foreign_balance += Decimal("1")
            live = await audit_replay.compare_with_live(db, snap)
            assert {m.field for m in live} >= {"restricted_gold", "treasury.foreign"}
            await db.rollback()
            events = await audit_replay.load_events(db)
            opening = next(e for e in events if e.payload.get("purpose") == "short_open")
            opening.payload = {**opening.payload, "short_after": {**opening.payload["short_after"], "principal_foreign": "999"}}
            _, errors = audit_replay.fold(events, check=True)
            assert any(m.field == "principal_foreign" for m in errors)
            opening.payload = {**opening.payload, "short_after": {**opening.payload["short_after"], "principal_foreign": "10"}}
            incomplete = dict(opening.payload)
            del incomplete["short_before"]
            opening.payload = incomplete
            with pytest.raises(ValueError, match="short replay snapshot"):
                audit_replay.fold(events, check=True)
        await _cover(uid, pid, cover_all=True)
    async with async_session_maker() as db:
        snap, errors = audit_replay.fold(await audit_replay.load_events(db), check=True)
        assert errors == []
        assert await audit_replay.compare_with_live(db, snap) == []
        assert snap.fx_shorts[uid, pid].interest_last_accrued_at is None

async def test_short_writeoff_replays_without_minting_treasury():
    uid, pid = await _seed(rate="0.01", sell_fee="0.01")
    await _open(uid, pid, "10")
    async with async_session_maker() as db:
        treasury = (await db.execute(select(FxTreasury))).scalars().one()
        stock = treasury.foreign_balance
        await db.rollback()
        await admin_user_service.writeoff_fx_short(db, target_id=uid, pair_id=pid, reason="irrecoverable", admin_id=uid)
        snap, errors = audit_replay.fold(await audit_replay.load_events(db), check=True)
        assert errors == []
        assert await audit_replay.compare_with_live(db, snap) == []
        assert snap.fx_shorts[uid, pid].principal_foreign == 0
        assert snap.fx_pairs[pid].treasury_foreign == stock
