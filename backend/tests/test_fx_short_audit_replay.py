"""Real ledger replay catches foreign debt/stock drift without double-booking gold."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
import pytest
from sqlalchemy import select
from app.core.database import async_session_maker
from app.models.fx import FxShortPosition, FxTreasury
from app.services import audit_replay, admin_user_service
from tests.conftest import publish_seeded_market_records
from tests.test_fx_short_trading import _seed, _open, _cover, _credit_flags
pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures('published_markets')]

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
            for event in events:
                if event.payload.get("purpose") in {"short_open", "short_cover"}:
                    assert "restricted_gold" not in event.payload["short_after"]
                    assert "released_lock" not in event.payload
                    assert "total_restricted_gold_before" not in event.payload
            opening = next(e for e in events if e.payload.get("purpose") == "short_open")
            cutoff, errors = audit_replay.fold(await audit_replay.load_events(db, upto_id=opening.id), check=True)
            assert errors == []
            assert cutoff.fx_shorts[uid, pid].principal_foreign == Decimal("10")
            position = (await db.execute(select(FxShortPosition))).scalars().one()
            treasury = (await db.execute(select(FxTreasury))).scalars().one()
            position.proceeds_basis_gold += Decimal("1")
            treasury.foreign_balance += Decimal("1")
            live = await audit_replay.compare_with_live(db, snap)
            assert {m.field for m in live} >= {"proceeds_basis_gold", "treasury.foreign"}
            await db.rollback()
            events = await audit_replay.load_events(db)
            opening = next(e for e in events if e.payload.get("purpose") == "short_open")
            opening.payload = {**opening.payload, "short_after": {**opening.payload["short_after"], "principal_foreign": "999"}}
            _, errors = audit_replay.fold(events, check=True)
            assert any(m.field == "principal_foreign" for m in errors)
            recorded, _ = audit_replay.fold([opening])
            assert recorded.fx_shorts[uid, pid].principal_foreign == Decimal("999")
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

@pytest.mark.parametrize("legacy_payload", [False, True])
async def test_short_writeoff_replays_without_minting_treasury(legacy_payload):
    uid, pid = await _seed(rate="0.01", sell_fee="0.01")
    await _open(uid, pid, "10")
    async with async_session_maker() as db:
        treasury = (await db.execute(select(FxTreasury))).scalars().one()
        stock = treasury.foreign_balance
        await db.rollback()
        await admin_user_service.writeoff_fx_short(db, target_id=uid, pair_id=pid,
                                                   reason="irrecoverable", admin_id=uid,
                                                   idempotency_key="audit-writeoff")
        events = await audit_replay.load_events(db)
        if legacy_payload:
            from copy import deepcopy
            opening = next(e for e in events if e.payload.get("purpose") == "short_open")
            writeoff = next(e for e in events if e.payload.get("purpose") == "short_writeoff")
            lock = opening.payload["gold_proceeds"]
            opening.payload = deepcopy(opening.payload)
            opening.payload["short_before"]["restricted_gold"] = "0"
            opening.payload["short_after"]["restricted_gold"] = lock
            writeoff.payload = deepcopy(writeoff.payload)
            writeoff.payload["short_before"]["restricted_gold"] = lock
            writeoff.payload["short_after"]["restricted_gold"] = "0"
            writeoff.payload["released_lock"] = lock
        snap, errors = audit_replay.fold(events, check=True)
        assert errors == []
        assert await audit_replay.compare_with_live(db, snap) == []
        assert snap.fx_shorts[uid, pid].principal_foreign == 0
        assert snap.fx_pairs[pid].treasury_foreign == stock

async def _add_sibling(uid, *, principal="0", basis="0"):
    from uuid import uuid4
    from app.models.fx import FxPair
    async with async_session_maker() as db:
        pair = FxPair(currency_code=uuid4().hex[:16], currency_name="sibling", status="trading", gold_reserve=Decimal("1000"),
                      foreign_reserve=Decimal("1000"), short_lending_limit_foreign=Decimal("100000"))
        db.add(pair)
        await db.flush()
        db.add(FxTreasury(pair_id=pair.id, gold_balance=Decimal("1000"), foreign_balance=Decimal("100000")))
        if Decimal(principal):
            db.add(FxShortPosition(user_id=uid, pair_id=pair.id, principal_foreign=Decimal(principal),
                                  proceeds_basis_gold=Decimal(basis),
                                  interest_last_accrued_at=datetime.now(timezone.utc)))
        await db.commit()
        await publish_seeded_market_records()
        return pair.id

async def test_open_clears_other_pair_pre_accrual_anchors():
    uid, a = await _seed(rate="0.01")
    b = await _add_sibling(uid)
    initial = datetime.now(timezone.utc)
    with patch("app.services.fx.shorts.utcnow", return_value=initial):
        await _open(uid, a, "10")
        await _open(uid, b, "10")
    with patch("app.services.fx.shorts.utcnow", return_value=initial + timedelta(days=1)):
        await _open(uid, a, "1")
        await _open(uid, b, "1")
    async with async_session_maker() as db:
        snap, errors = audit_replay.fold(await audit_replay.load_events(db), check=True)
        assert errors == []
        assert await audit_replay.compare_with_live(db, snap) == []

async def test_legacy_cover_then_unlocked_cover_preserves_economic_replay():
    """Old lock snapshots must join new cover events without losing debt/basis checks."""
    from copy import deepcopy
    uid, pid = await _seed(cash="100", buy_fee="0.01")
    await _open(uid, pid, "10")
    await _cover(uid, pid, q="3")
    await _cover(uid, pid, q="2")
    async with async_session_maker() as db:
        events = await audit_replay.load_events(db)
        opening = next(e for e in events if e.payload.get("purpose") == "short_open")
        covers = [e for e in events if e.payload.get("purpose") == "short_cover"]
        # Reconstruct the old persisted payload shape in memory only. Historical
        # locks were part of existing cash, so no economic balances change.
        legacy_lock = Decimal(opening.payload["gold_proceeds"])
        opening.payload = deepcopy(opening.payload)
        opening.payload["short_before"]["restricted_gold"] = "0"
        opening.payload["short_after"]["restricted_gold"] = str(legacy_lock)
        cover = covers[0]
        cover.payload = deepcopy(cover.payload)
        # An unseen sibling can earmark the remaining cash in old history;
        # the cover then releases its actual cost above proportional basis.
        total_lock = cover.payload["user_before"]["cash"]
        release = Decimal(cover.payload["paid_gold"])
        assert release > Decimal(cover.payload["allocated_proceeds_basis"])
        cover.payload["short_before"]["restricted_gold"] = str(legacy_lock)
        cover.payload["short_after"]["restricted_gold"] = str(legacy_lock - release)
        cover.payload["total_restricted_gold_before"] = str(total_lock)
        cover.payload["released_lock"] = str(release)
        for check in (False, True):
            cutoff, errors = audit_replay.fold([e for e in events if e.id <= cover.id], check=check)
            assert errors == []
            assert cutoff.fx_shorts[uid, pid].restricted_gold == legacy_lock - release
            snap, errors = audit_replay.fold(events, check=check)
            assert errors == []
            assert snap.fx_shorts[uid, pid].restricted_gold is None
            assert await audit_replay.compare_with_live(db, snap) == []
        cover.payload.pop("total_restricted_gold_before")
        with pytest.raises(ValueError, match="total_restricted_gold_before"):
            audit_replay.fold(events, check=True)
        cover.payload["total_restricted_gold_before"] = str(total_lock)
        covers[1].payload = {**covers[1].payload, "short_after": {
            **covers[1].payload["short_after"], "proceeds_basis_gold": "999"}}
        _, errors = audit_replay.fold(events, check=True)
        assert any(m.field == "proceeds_basis_gold" for m in errors)
        covers[1].ts += timedelta(seconds=1)
        _, errors = audit_replay.fold(events, check=True)
        assert any(m.field == "accrued_at" for m in errors)

async def test_short_replay_rejects_missing_economic_snapshots():
    uid, pid = await _seed()
    await _open(uid, pid, "10")
    async with async_session_maker() as db:
        events = await audit_replay.load_events(db)
        event = next(e for e in events if e.payload.get("purpose") == "short_open")
        original = event.user_after
        event.user_after = None
        with pytest.raises(ValueError, match="user_after"):
            audit_replay.fold(events, check=True)
        event.user_after = original
        event.payload = {k: v for k, v in event.payload.items() if k != "treasury_after"}
        with pytest.raises(ValueError, match="treasury_after"):
            audit_replay.fold(events, check=True)
