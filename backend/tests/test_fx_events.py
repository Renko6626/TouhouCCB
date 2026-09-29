from decimal import Decimal

from app.models.fx import FxEvent, FxTrade
from app.models.audit import AuditEvent
from app.services.fx import scheduler
from app.services.fx.engine import _NoMove
from tests.fx_test_helpers import add_pair, fx_db
from sqlalchemy import select
from datetime import datetime, timezone
import pytest


def test_event_defaults_are_safe():
    event = FxEvent(pair_id=1, title="x", kind="macro")
    assert event.status == "draft"
    assert Decimal(event.budget or 0) == 0


@pytest.mark.asyncio
async def test_publish_event_records_first_trade_snapshot_and_is_idempotent(fx_db, monkeypatch):
    async def no_publish(_trade): return None
    monkeypatch.setattr("app.services.fx.market_data.publish_trade", no_publish)
    pair, _ = await add_pair(fx_db, code="EVT")
    event = FxEvent(pair_id=pair.id, title="shock", kind="macro", shock_ratio=Decimal("0.001"),
                    first_reaction_ratio=Decimal("0.25"), window_sec=600, budget=Decimal("10"))
    fx_db.add(event); await fx_db.commit()
    published = await scheduler.publish_event(fx_db, event.id, datetime.now(timezone.utc))
    assert published.status == "published"
    saved = await fx_db.get(FxEvent, event.id)
    assert saved.parameter_snapshot["first_trade_id"]
    assert Decimal(saved.parameter_snapshot["spent"]) > 0
    first_snapshot = dict(saved.parameter_snapshot)
    first_trades = (await fx_db.execute(select(FxTrade).where(FxTrade.pair_id == pair.id))).scalars().all()
    assert len(first_trades) == 1
    assert first_trades[0].id == first_snapshot["first_trade_id"]
    retry = await scheduler.publish_event(fx_db, event.id, datetime.now(timezone.utc))
    assert retry.id == published.id
    assert retry.id == saved.id
    await fx_db.refresh(saved)
    assert saved.parameter_snapshot == first_snapshot
    assert len((await fx_db.execute(select(FxTrade).where(FxTrade.pair_id == pair.id))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_publish_event_failure_persists_cancel_and_audit(fx_db, monkeypatch):
    pair, _ = await add_pair(fx_db, code="EVFAIL")
    event = FxEvent(pair_id=pair.id, title="fail", kind="macro", shock_ratio=Decimal("0.001"),
                    first_reaction_ratio=Decimal("0.25"), window_sec=600, budget=Decimal("10"))
    fx_db.add(event); await fx_db.commit()
    async def fail(*args, **kwargs): return _NoMove("forced")
    monkeypatch.setattr(scheduler.ENGINE, "_safe_system_move", fail)
    with pytest.raises(Exception):
        await scheduler.publish_event(fx_db, event.id, datetime.now(timezone.utc))
    saved = await fx_db.get(FxEvent, event.id)
    assert saved.status == "cancelled"
    audit = (await fx_db.execute(select(AuditEvent).where(AuditEvent.event_type == "fx_event_cancel"))).scalars().all()
    assert audit


@pytest.mark.asyncio
async def test_tick_completes_active_event_when_budget_is_spent(fx_db, monkeypatch):
    async def no_publish(_trade): return None
    monkeypatch.setattr("app.services.fx.market_data.publish_trade", no_publish)
    pair, _ = await add_pair(fx_db, code="EVCOMP")
    event = FxEvent(pair_id=pair.id, title="complete", kind="macro", shock_ratio=Decimal("0.001"),
                    first_reaction_ratio=Decimal("0.25"), window_sec=600, budget=Decimal("10"))
    fx_db.add(event); await fx_db.commit()
    published = await scheduler.publish_event(fx_db, event.id, datetime.now(timezone.utc))
    saved = await fx_db.get(FxEvent, event.id)
    saved.parameter_snapshot = {**saved.parameter_snapshot, "spent": str(saved.budget)}
    await fx_db.commit()
    from app.services.fx.engine import FxEngine
    result = await FxEngine(session_factory=lambda: fx_db).tick(datetime.now(timezone.utc))
    assert (await fx_db.get(FxEvent, event.id)).status == "completed"
    assert any("event_budget_exhausted" in reason for reason in result.reasons)


@pytest.mark.asyncio
@pytest.mark.parametrize("shock,kind,ratio", [
    (Decimal("0.02"), "macro", Decimal("0.25")),
    (Decimal("-0.02"), "macro", Decimal("0.25")),
    (Decimal("0.08"), "black_swan", Decimal("0.7")),
])
async def test_event_publish_then_tick_moves_toward_saved_target_without_overshoot(fx_db, monkeypatch, shock, kind, ratio):
    from datetime import timedelta
    from app.services.fx.engine import FxEngine

    async def no_publish(_trade): return None
    monkeypatch.setattr("app.services.fx.market_data.publish_trade", no_publish)
    pair, _ = await add_pair(fx_db, code="DIRECTION")
    event = FxEvent(pair_id=pair.id, title="direction", kind=kind, shock_ratio=shock,
                    first_reaction_ratio=ratio, window_sec=180, budget=Decimal("100"))
    fx_db.add(event); await fx_db.commit()
    now = datetime.now(timezone.utc)
    published = await scheduler.publish_event(fx_db, event.id, now)
    await fx_db.refresh(pair)
    target = Decimal("1") + shock
    assert pair.target_price == target
    assert Decimal(published.parameter_snapshot["target_after"]) == target
    first_price = pair.gold_reserve / pair.foreign_reserve
    assert min(Decimal("1"), target) < first_price < max(Decimal("1"), target)

    engine = FxEngine(session_factory=lambda: fx_db)
    await engine.tick(now + timedelta(seconds=90))
    await fx_db.refresh(pair)
    middle_price = pair.gold_reserve / pair.foreign_reserve
    assert min(first_price, target) < middle_price < max(first_price, target)
    await engine.tick(now + timedelta(seconds=180))
    await fx_db.refresh(pair)
    final_price = pair.gold_reserve / pair.foreign_reserve
    assert min(middle_price, target) <= final_price <= max(middle_price, target)
    assert abs(final_price - target) < abs(first_price - target)
    saved = await fx_db.get(FxEvent, event.id)
    assert saved.status == "completed"
    assert saved.parameter_snapshot["last_tick_at"]


@pytest.mark.asyncio
async def test_schedule_rejects_overlap_with_published_window_without_mutating_draft(fx_db, monkeypatch):
    async def no_publish(_trade): return None
    monkeypatch.setattr("app.services.fx.market_data.publish_trade", no_publish)
    pair, _ = await add_pair(fx_db, code="SCHEDLOCK")
    now = datetime.now(timezone.utc)
    active = FxEvent(pair_id=pair.id, title="active", kind="macro", shock_ratio=Decimal("0.01"),
                     first_reaction_ratio=Decimal("0.25"), window_sec=180, budget=Decimal("10"))
    draft = FxEvent(pair_id=pair.id, title="draft", kind="macro", shock_ratio=Decimal("0.01"),
                    first_reaction_ratio=Decimal("0.25"), window_sec=180, budget=Decimal("10"))
    fx_db.add_all([active, draft]); await fx_db.commit()
    await scheduler.publish_event(fx_db, active.id, now)
    statements = []
    original_execute = fx_db.execute
    async def record_execute(statement):
        statements.append(statement)
        return await original_execute(statement)
    monkeypatch.setattr(fx_db, "execute", record_execute)
    with pytest.raises(Exception) as exc:
        await scheduler.schedule_event(fx_db, draft.id, now + __import__("datetime").timedelta(seconds=60))
    assert getattr(exc.value, "status_code", None) == 409
    saved = await fx_db.get(FxEvent, draft.id)
    assert saved.status == "draft"
    assert saved.scheduled_at is None
    assert any("fx_pair" in str(statement).lower() for statement in statements)
