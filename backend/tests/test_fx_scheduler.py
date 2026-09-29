from app.services.fx import scheduler
from app.services.fx.engine import FxEngine
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import pytest
from sqlalchemy import event as sa_event
from app.models.fx import FxEvent
from tests.fx_test_helpers import add_pair, fx_db


def test_scheduler_runs_every_five_seconds():
    assert scheduler._JOB_ID == "fx-engine"


def test_noise_clock_is_initialized_once_and_not_reset_from_target_clock():
    engine = FxEngine()
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    engine._last_noise_at[7] = t0
    engine._last_target_at[7] = t0 + timedelta(seconds=5)
    assert engine._last_noise_at[7] == t0


def test_scheduler_exposes_shared_engine_instance():
    assert scheduler.ENGINE is scheduler.ENGINE
    scheduler.ENGINE._last_noise_at[7] = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert scheduler.ENGINE._last_noise_at[7].year == 2026


@pytest.mark.asyncio
async def test_scheduler_processes_due_events_in_utc_order_and_honors_gate(monkeypatch, fx_db):
    pair, _ = await add_pair(fx_db, code="SCHED")
    now = datetime.now(timezone.utc)
    late = FxEvent(pair_id=pair.id, title="late", kind="macro", status="scheduled", scheduled_at=now - timedelta(seconds=1))
    early = FxEvent(pair_id=pair.id, title="early", kind="macro", status="scheduled", scheduled_at=now - timedelta(seconds=2))
    fx_db.add_all([late, early]); await fx_db.commit()
    monkeypatch.setattr(scheduler, "async_session_maker", lambda: fx_db)
    seen = []
    async def publish(db, event_id): seen.append(event_id)
    async def engine_tick(): return None
    monkeypatch.setattr(scheduler, "publish_event", publish)
    monkeypatch.setattr(scheduler.ENGINE, "tick", engine_tick)
    await scheduler._tick_safe()
    assert seen == [early.id, late.id]

    seen.clear()
    async def gate_off(db, key, default): return False
    monkeypatch.setattr(scheduler.site_config, "get_bool_or", gate_off)
    await scheduler._tick_safe()
    assert seen == []


# ── I3 lock order: every event path locks fx_pair before fx_event ──

def _record_lock_order(db):
    order: list[str] = []

    def record(state):
        statement = state.statement
        if getattr(statement, "_for_update_arg", None) is None:
            return
        descriptions = statement.column_descriptions
        entity = descriptions[0].get("entity") if descriptions else None
        name = getattr(entity, "__tablename__", None)
        if name:
            order.append(name)

    sa_event.listen(db._session, "do_orm_execute", record)
    return order, record


@pytest.mark.asyncio
async def test_schedule_event_locks_pair_before_event(fx_db):
    pair, _ = await add_pair(fx_db, code="LOCKA")
    draft = FxEvent(pair_id=pair.id, title="draft", kind="macro", status="draft", window_sec=180)
    fx_db.add(draft)
    await fx_db.commit()
    order, record = _record_lock_order(fx_db)
    try:
        await scheduler.schedule_event(fx_db, draft.id,
                                       datetime.now(timezone.utc) + timedelta(minutes=5))
    finally:
        sa_event.remove(fx_db._session, "do_orm_execute", record)
    # SQLite cannot prove concurrency, but the emitted lock order must match
    # engine.tick (pair first) to avoid PostgreSQL deadlocks.
    assert order[:2] == ["fx_pair", "fx_event"]


@pytest.mark.asyncio
async def test_publish_event_locks_pair_before_event(fx_db, monkeypatch):
    async def no_publish(_trade): return None
    monkeypatch.setattr("app.services.fx.market_data.publish_trade", no_publish)
    pair, _ = await add_pair(fx_db, code="LOCKB", target="1")
    event = FxEvent(pair_id=pair.id, title="evt", kind="macro", status="draft",
                    shock_ratio=Decimal("0.01"), first_reaction_ratio=Decimal("0.25"),
                    window_sec=180, budget=Decimal("10"))
    fx_db.add(event)
    await fx_db.commit()
    order, record = _record_lock_order(fx_db)
    try:
        await scheduler.publish_event(fx_db, event.id)
    finally:
        sa_event.remove(fx_db._session, "do_orm_execute", record)
    assert order[:2] == ["fx_pair", "fx_event"]
