from decimal import Decimal

from app.services.fx.randomness import FxRandomSource
from app.services.fx.engine import event_progress, normal_target_progress, _NoMove
from app.services.fx.engine import FxEngine
from app.models.base import SiteConfig
from app.models.fx import FxTrade, FxTreasury
from app.services import site_config
from tests.fx_test_helpers import add_pair, fx_db
from sqlalchemy import select
import pytest
from datetime import datetime, timezone, timedelta


def test_target_step_is_deterministic_with_injected_rng():
    class R:
        def gauss(self, _mu, _sigma):
            return 0.1
    one = FxRandomSource.step_target(Decimal("0"), 3600, Decimal("0.002"), R())
    two = FxRandomSource.step_target(Decimal("0"), 3600, Decimal("0.002"), R())
    assert one == two and one > 0


def test_event_window_progress_reaches_about_95_percent():
    assert Decimal("0.94") < event_progress(600, 600) < Decimal("0.96")


def test_normal_target_progress_reaches_half_at_half_life():
    assert Decimal("0.49") < normal_target_progress(600, 600) < Decimal("0.51")


def test_quote_failure_is_structured_no_move():
    failure = _NoMove("quote_failed")
    assert not failure and failure.reason == "quote_failed"


def test_runtime_failure_reason_is_structured():
    failure = _NoMove("exception:RuntimeError")
    assert failure.reason.startswith("exception:")


@pytest.mark.asyncio
async def test_system_move_conserves_each_currency_total(fx_db):
    pair, treasury = await add_pair(fx_db, code="CONS")
    engine = FxEngine()
    before_gold = pair.gold_reserve + treasury.gold_balance
    before_foreign = pair.foreign_reserve + treasury.foreign_balance
    bought = await engine._system_move(fx_db, pair, treasury, Decimal("1.1"), Decimal("0"),
                                       Decimal("100"), source="test", now=datetime.now(timezone.utc))
    assert bought and pair.gold_reserve > Decimal("100")
    assert treasury.gold_balance < Decimal("100")
    sold = await engine._system_move(fx_db, pair, treasury, Decimal("0.9"), Decimal("0"),
                                     Decimal("100"), source="test", now=datetime.now(timezone.utc))
    assert sold and treasury.foreign_balance < Decimal("100")
    assert pair.gold_reserve + treasury.gold_balance == before_gold
    assert pair.foreign_reserve + treasury.foreign_balance == before_foreign
    assert treasury.daily_spend > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("desired", [Decimal("1.1"), Decimal("0.9")])
async def test_system_move_respects_daily_and_event_budget(fx_db, desired):
    pair, treasury = await add_pair(fx_db, code="BUDGET")
    engine = FxEngine()
    now = datetime.now(timezone.utc)
    before_spend = treasury.daily_spend
    move = await engine._system_move(fx_db, pair, treasury, desired,
                                     Decimal("1"), Decimal("1"), source="event", now=now)
    assert move and Decimal("0") < move.actual_spend <= Decimal("1")
    assert treasury.daily_spend - before_spend == move.actual_spend
    assert move.trade.side == ("buy" if desired > 1 else "sell")
    await fx_db.commit()
    await fx_db.refresh(pair)
    await fx_db.refresh(treasury)
    assert treasury.daily_spend <= Decimal("1")

    state = (pair.gold_reserve, pair.foreign_reserve, pair.pool_version,
             treasury.gold_balance, treasury.foreign_balance, treasury.daily_spend)
    count = len((await fx_db.execute(select(FxTrade))).scalars().all())
    blocked = await engine._system_move(fx_db, pair, treasury, desired,
                                        Decimal("1"), treasury.daily_spend,
                                        source="event", now=now)
    assert not blocked and blocked.reason == "daily_budget_exhausted"
    await fx_db.commit()
    await fx_db.refresh(pair)
    await fx_db.refresh(treasury)
    assert state == (pair.gold_reserve, pair.foreign_reserve, pair.pool_version,
                     treasury.gold_balance, treasury.foreign_balance, treasury.daily_spend)
    assert len((await fx_db.execute(select(FxTrade))).scalars().all()) == count


@pytest.mark.asyncio
async def test_tick_records_budget_and_invalid_target_reasons_and_processes_other_pair(fx_db):
    bad, _ = await add_pair(fx_db, code="BAD", target="3", target_min="3", target_max="4")
    good, treasury = await add_pair(fx_db, code="GOOD", target="2")
    treasury.gold_balance = Decimal("0")
    await fx_db.commit()
    engine = FxEngine(session_factory=lambda: fx_db)
    result = await engine.tick(datetime.now(timezone.utc))
    assert result.pairs == 2
    assert any("invalid_target_bounds" in reason for reason in result.reasons)
    assert any(":normal:" in reason for reason in result.reasons)


@pytest.mark.asyncio
async def test_tick_savepoint_failure_does_not_abort_other_pairs(fx_db, monkeypatch):
    first, _ = await add_pair(fx_db, code="FAIL", target="2")
    second, _ = await add_pair(fx_db, code="PASS", target="2")
    engine = FxEngine(session_factory=lambda: fx_db)
    old = datetime.now(timezone.utc).replace(year=2025)
    engine._last_target_at[first.id] = old
    engine._last_target_at[second.id] = old
    original = engine._system_move
    calls = 0

    async def fail_once(db, pair, *args, **kwargs):
        nonlocal calls
        calls += 1
        if pair.id == first.id:
            pair.gold_reserve = Decimal("999")
            raise RuntimeError("forced")
        return await original(db, pair, *args, **kwargs)

    monkeypatch.setattr(engine, "_system_move", fail_once)
    async def no_publish(_trade): return None
    monkeypatch.setattr("app.services.fx.market_data.publish_trade", no_publish)
    result = await engine.tick(datetime.now(timezone.utc))
    await fx_db.refresh(first)
    await fx_db.refresh(second)
    assert calls >= 2
    assert any("exception:RuntimeError" in reason for reason in result.reasons)
    assert second.gold_reserve != Decimal("100")
    assert first.gold_reserve == Decimal("100")
    trades = (await fx_db.execute(select(FxTrade))).scalars().all()
    assert any(trade.pair_id == second.id for trade in trades)
    assert not any(trade.pair_id == first.id for trade in trades)


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_fails", [False, True])
async def test_system_trade_publishes_only_after_commit(fx_db, monkeypatch, commit_fails):
    pair, _ = await add_pair(fx_db, code="ORDER", target="2")
    engine = FxEngine(session_factory=lambda: fx_db)
    committed = False
    published = []
    original_commit = fx_db.commit

    async def tracked_commit():
        nonlocal committed
        if commit_fails:
            raise RuntimeError("commit failed")
        await original_commit()
        committed = True

    async def record_publish(trade):
        assert committed
        saved = await fx_db.get(FxTrade, trade.id)
        assert saved is not None and saved.id == trade.id
        published.append(trade.id)

    monkeypatch.setattr(fx_db, "commit", tracked_commit)
    monkeypatch.setattr("app.services.fx.market_data.publish_trade", record_publish)
    if commit_fails:
        with pytest.raises(RuntimeError, match="commit failed"):
            await engine.tick(datetime.now(timezone.utc))
        assert published == []
        assert (await fx_db.execute(select(FxTrade))).scalars().all() == []
    else:
        await engine.tick(datetime.now(timezone.utc))
        assert published


@pytest.mark.asyncio
async def test_tick_clips_target_to_configured_and_safety_intersection(fx_db, monkeypatch):
    async def no_publish(_trade): return None
    monkeypatch.setattr("app.services.fx.market_data.publish_trade", no_publish)
    pair, _ = await add_pair(fx_db, code="CLIP", target="1.5", initial="1", target_min="1.2", target_max="1.8")
    class High:
        def gauss(self, _mu, _sigma): return 100
    engine = FxEngine(session_factory=lambda: fx_db)
    await engine.tick(datetime.now(timezone.utc), rng=High())
    assert Decimal("1.2") <= pair.target_price <= Decimal("1.8")


@pytest.mark.asyncio
async def test_noise_uses_random_interval_and_independent_direction(fx_db, monkeypatch):
    async def no_publish(_trade): return None
    monkeypatch.setattr("app.services.fx.market_data.publish_trade", no_publish)
    pair, _ = await add_pair(fx_db, code="NOISE")

    class Source:
        def __init__(self): self.directions = iter((1.0, 0.0, 1.0))
        def gauss(self, _mu, _sigma): return 0.0
        def expovariate(self, _rate): return 0.0
        def random(self): return next(self.directions)

    source = Source()
    engine = FxEngine(session_factory=lambda: fx_db)
    now = datetime.now(timezone.utc)
    await engine.tick(now, rng=source)
    await engine.tick(now.replace(microsecond=0) + __import__("datetime").timedelta(seconds=1), rng=source)
    await engine.tick(now.replace(microsecond=0) + __import__("datetime").timedelta(seconds=2), rng=source)
    trades = (await fx_db.execute(select(FxTrade).where(FxTrade.pair_id == pair.id, FxTrade.source == "system_noise").order_by(FxTrade.id))).scalars().all()
    assert len(trades) >= 2
    assert {trade.side for trade in trades[:2]} == {"buy", "sell"}


# ── I2 config-driven normal intervention cap ──

class _QuietSource:
    """Deterministic source: target random walk holds, noise never fires."""

    def gauss(self, _mu, _sigma):
        return 0.0

    def expovariate(self, _rate):
        return 1e9

    def random(self):
        return 1.0


async def _set_move_limit(db, value):
    row = (await db.execute(select(SiteConfig).where(
        SiteConfig.key == "fx_default_price_move_limit"))).scalars().first()
    if row is None:
        db.add(SiteConfig(key="fx_default_price_move_limit", value=str(value), value_type="decimal"))
    else:
        row.value = str(value)
    await db.commit()
    site_config.clear_cache()


@pytest.mark.asyncio
async def test_normal_intervention_size_follows_configured_move_limit(fx_db, monkeypatch):
    async def no_publish(_trade): return None
    monkeypatch.setattr("app.services.fx.market_data.publish_trade", no_publish)

    async def normal_spend(limit: Decimal, code: str) -> Decimal:
        pair, _ = await add_pair(fx_db, code=code, target="2")
        await _set_move_limit(fx_db, limit)
        engine = FxEngine(session_factory=lambda: fx_db)
        now = datetime.now(timezone.utc)
        # Force a long elapsed time so the normal controller wants to sprint to target.
        engine._last_target_at[pair.id] = now - timedelta(hours=1)
        await engine.tick(now, rng=_QuietSource())
        trades = (await fx_db.execute(select(FxTrade).where(
            FxTrade.pair_id == pair.id, FxTrade.source == "system_target"))).scalars().all()
        return sum((t.input_amount for t in trades), Decimal("0"))

    small = await normal_spend(Decimal("0.001"), "MOVS")
    large = await normal_spend(Decimal("0.1"), "MOVL")
    assert small > 0
    assert large > small

