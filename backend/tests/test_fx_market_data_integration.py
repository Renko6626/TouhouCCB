"""Task 3 integration: real commit hooks and event-history generation.

Asserts only observable behaviour on the real async database:

* a real player commit through the request wrapper reaches the incremental
  runtime (proved by a long reconcile interval: only the post-commit hint can
  wake the consumer in time), and replay / rejection / in-session rollback
  leave the derived candles untouched;
* event materialization reproduces a fresh full aggregation exactly, rotates the history
  generation.

The storage layer and the runtime ring are covered by their own suites; this
file only covers the wiring between them.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url
from fastapi import HTTPException
from sqlalchemy import delete, func, select

from app.core.database import async_session_maker
from app.models.base import SiteConfig, User
from app.models.audit import AuditEvent
from app.services.fx.pool_events import append_pool_event
from app.models.fx import FxCandle, FxMarketDataState, FxPair, FxPoolEvent, FxTrade, FxTreasury
from app.services import site_config
from app.services.credit import flags as credit_flags
from app.services.fx import trading
from app.services.fx.candles import FxCandleCursorConflict, apply_candle_batch
from app.services.fx.market_state import FX_MARKET_DATA
from tests.fx_test_helpers import backfill_fx_history

UTC = timezone.utc
BASE = datetime(2026, 10, 1, 9, 0, 0, tzinfo=UTC)


@pytest.fixture(scope="session", autouse=True)
def _guard_pg_test_database():
    """Reject unsafe URLs before function-scoped setup_db can replace tables."""
    from app.core.database import engine

    if engine.dialect.name == "postgresql":
        database = make_url(str(engine.url)).database or ""
        if not (database.endswith("_test") or database.startswith("test_")):
            raise RuntimeError("FX integration ownership requires a disposable test database")


@pytest_asyncio.fixture(autouse=True)
async def _pg_service_write_ownership(_guard_pg_test_database, setup_db):
    """Direct service tests have no lifespan; acquire its real PG writer lock."""
    from app.core.database import engine
    from app.services.credit.ownership import OWNERSHIP

    if engine.dialect.name != "postgresql":
        yield
        return
    await OWNERSHIP.acquire(required=True)
    try:
        yield
    finally:
        await OWNERSHIP.release()


async def _enable_fx() -> None:
    async with async_session_maker() as db:
        row = (await db.execute(
            select(SiteConfig).where(SiteConfig.key == "fx_enabled")
        )).scalars().first()
        if row is None:
            db.add(SiteConfig(key="fx_enabled", value="true", value_type="bool"))
        else:
            row.value = "true"
        await db.commit()
    site_config.clear_cache()


async def _seed_pair(*, code: str = "ITG", status: str = "trading",
                     archived: bool = False, initial_at: datetime | None = None) -> int:
    async with async_session_maker() as db:
        pair = FxPair(currency_code=code, currency_name="Integration", status=status,
                      archived=archived, gold_reserve=Decimal("1000"),
                      foreign_reserve=Decimal("1000"), buy_fee_rate=Decimal("0"),
                      sell_fee_rate=Decimal("0"))
        db.add(pair)
        await db.flush()
        initial_at = initial_at or datetime.now(UTC).replace(second=0, microsecond=0)
        audit = AuditEvent(event_type="fx_fund", ref_table="fx_pair", ref_id=pair.id, ts=initial_at, payload={"action": "initial_issuance", "pool_after": {"gold": "1000", "foreign": "1000"}})
        db.add(audit)
        await db.flush()
        append_pool_event(db, pair_id=pair.id, kind="initialize", created_at=initial_at,
                          pre_reserves=None, post_reserves=(Decimal("1000"), Decimal("1000")),
                          gold_volume=Decimal("0"), audit_id=audit.id)
        db.add(FxTreasury(pair_id=pair.id, gold_balance=Decimal("1000"),
                          foreign_balance=Decimal("1000")))
        await db.commit()
        return int(pair.id)


async def _seed_user(*, cash: str = "1000") -> int:
    async with async_session_maker() as db:
        user = User(username=f"itg-{datetime.now(UTC).timestamp()}",
                    cash=Decimal(cash), tos_accepted_at=datetime.now(UTC))
        db.add(user)
        await db.commit()
        return int(user.id)


async def _add_raw_trade(pair_id: int, *, at: datetime, price: str, side: str = "buy",
                         input_amount: str = "2", output_amount: str = "1") -> int:
    async with async_session_maker() as db:
        trade = FxTrade(pair_id=pair_id, side=side,
                        input_amount=Decimal(input_amount),
                        output_amount=Decimal(output_amount),
                        pre_gold_reserve=(Decimal(price) - Decimal("0.10")) * 1000 if Decimal(price) > 1 else Decimal("1000"),
                        pre_foreign_reserve=Decimal("1000"),
                        post_gold_reserve=Decimal(price) * 1000,
                        post_foreign_reserve=Decimal("1000"),
                        post_price=Decimal(price), created_at=at, source="player")
        db.add(trade)
        await db.flush()
        trade_id = int(trade.id)
        pair = await db.get(FxPair, pair_id)
        append_pool_event(db, pair_id=pair_id, kind="trade", created_at=at,
                          pre_reserves=(trade.pre_gold_reserve, trade.pre_foreign_reserve),
                          post_reserves=(trade.post_gold_reserve, trade.post_foreign_reserve),
                          gold_volume=trade.input_amount if side == "buy" else trade.output_amount,
                          trade_id=trade_id)
        pair.gold_reserve = trade.post_gold_reserve
        await db.commit()
        return trade_id


async def _max_trade_id(pair_id: int) -> int:
    async with async_session_maker() as db:
        return int((await db.execute(
            select(func.max(FxTrade.id)).where(FxTrade.pair_id == pair_id)
        )).scalar() or 0)


async def _candle(pair_id: int, interval: str, bucket_start: datetime) -> dict | None:
    async with async_session_maker() as db:
        row = (await db.execute(select(FxCandle).where(
            FxCandle.pair_id == pair_id, FxCandle.interval == interval,
            FxCandle.bucket_start == bucket_start,
        ))).scalars().first()
        if row is None:
            return None
        return {
            "open": Decimal(row.open_price), "high": Decimal(row.high_price),
            "low": Decimal(row.low_price), "close": Decimal(row.close_price),
            "volume": Decimal(row.gold_volume), "n": int(row.n_trades),
            "first_id": int(row.first_event_id), "last_id": int(row.last_event_id),
        }


async def _candle_count(pair_id: int) -> int:
    async with async_session_maker() as db:
        return int((await db.execute(
            select(func.count()).select_from(FxCandle).where(FxCandle.pair_id == pair_id)
        )).scalar_one())


async def _state(pair_id: int) -> dict | None:
    async with async_session_maker() as db:
        row = (await db.execute(select(FxMarketDataState).where(
            FxMarketDataState.pair_id == pair_id
        ))).scalars().first()
        if row is None:
            return None
        return {"last_event_id": int(row.last_event_id),
                "history_version": str(row.history_version),
                "history_ready": bool(row.history_ready)}


async def _wait_applied(pair_id: int, trade_id: int, timeout: float = 5.0) -> None:
    async with async_session_maker() as db:
        event_id = (await db.execute(select(FxPoolEvent.id).where(FxPoolEvent.trade_id == trade_id))).scalar_one()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        state = FX_MARKET_DATA.state(pair_id)
        if state is not None and state["applied_event_id"] >= event_id:
            return
        if loop.time() > deadline:
            pytest.fail("FX market-data runtime never consumed the committed trade")
        await asyncio.sleep(0.02)


# ── commit / replay / rollback wiring ─────────────────────────────────────
@pytest.mark.asyncio
async def test_player_commit_wakes_runtime_and_replay_rollback_rejection_do_not_count():
    credit_flags.clear_flags()
    user_id = await _seed_user()
    pair_id = await _seed_pair()
    await _enable_fx()

    # 30s reconcile: only the post-commit notify can wake the consumer in time.
    default_interval = type(FX_MARKET_DATA).RECONCILE_INTERVAL
    FX_MARKET_DATA.RECONCILE_INTERVAL = 30.0
    await FX_MARKET_DATA.start(write_owner=True)
    try:
        async with async_session_maker() as db:
            public = await trading.execute_trade(
                db, user_id, pair_id, "buy", Decimal("10"), Decimal("0"), "itg-1")
        await _wait_applied(pair_id, public.id)
        await FX_MARKET_DATA.flush_once()

        # The trade is stamped "now"; read whichever single 1m bucket exists.
        rows = await _all_1m_candles(pair_id)
        assert len(rows) == 1
        assert rows[0]["volume"] == public.input_amount
        assert rows[0]["n"] == 1
        assert rows[0]["open"] == Decimal("1.00")
        assert rows[0]["close"] == Decimal(public.post_price)

        # Ready history wiring: get_public_snapshot uses the exact helper and
        # carries the real generation/readiness instead of the SQL fallback.
        async with async_session_maker() as db:
            snapshot = await trading.get_public_snapshot(db, pair_id)
        assert snapshot.volume_24h == public.input_amount
        assert snapshot.history_ready is True
        assert snapshot.history_version == FX_MARKET_DATA.state(pair_id)["history_version"]

        # Replay of the same idempotency key must not add volume or a trade.
        async with async_session_maker() as db:
            replay = await trading.execute_trade(
                db, user_id, pair_id, "buy", Decimal("10"), Decimal("0"), "itg-1")
        assert replay.id == public.id
        await FX_MARKET_DATA.catch_up(pair_id)
        await FX_MARKET_DATA.flush_once()
        assert (await _all_1m_candles(pair_id))[0]["volume"] == public.input_amount
        assert (await _all_1m_candles(pair_id))[0]["n"] == 1

        # In-session execution followed by a rollback must leave nothing behind.
        from app.services.credit.gates import GATES
        from app.services.credit.keys import GroupKey
        async with GATES.hold(exclusive=[GroupKey("fx", pair_id)]):
            async with async_session_maker() as db:
                execution = await trading.execute_trade_in_session(
                    db, user_id, pair_id, "buy", Decimal("5"), Decimal("0"), "itg-2")
                assert execution.replay is False
                await db.rollback()
        await FX_MARKET_DATA.catch_up(pair_id)
        await FX_MARKET_DATA.flush_once()
        assert (await _all_1m_candles(pair_id))[0]["n"] == 1

        # A rejected trade cannot be aggregated.
        with pytest.raises(HTTPException):
            async with async_session_maker() as db:
                await trading.execute_trade(
                    db, user_id, pair_id, "buy", Decimal("1000000"), Decimal("0"), "itg-3")
        await FX_MARKET_DATA.catch_up(pair_id)
        await FX_MARKET_DATA.flush_once()
        assert (await _all_1m_candles(pair_id))[0]["n"] == 1
    finally:
        await FX_MARKET_DATA.stop()
        FX_MARKET_DATA.RECONCILE_INTERVAL = default_interval


async def _all_1m_candles(pair_id: int) -> list[dict]:
    async with async_session_maker() as db:
        rows = (await db.execute(select(FxCandle).where(
            FxCandle.pair_id == pair_id, FxCandle.interval == "1m"
        ))).scalars().all()
        return [{"open": Decimal(r.open_price), "close": Decimal(r.close_price),
                 "volume": Decimal(r.gold_volume), "n": int(r.n_trades)} for r in rows]


# ── event materialization ───────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_event_history_materialization_rotates_generation_and_rejects_stale_flush():
    pair_id = await _seed_pair(code="RB", initial_at=BASE)
    await _add_raw_trade(pair_id, at=BASE + timedelta(seconds=30), price="1.30", input_amount="1")
    await _add_raw_trade(pair_id, at=BASE + timedelta(seconds=10), price="1.10", input_amount="2")
    await _add_raw_trade(pair_id, at=BASE + timedelta(seconds=20), price="1.20",
                         side="sell", input_amount="1", output_amount="3")
    through = await _max_trade_id(pair_id)

    async with async_session_maker() as db:
        await backfill_fx_history(db, pair_id)
    cutoff = (await _state(pair_id))["last_event_id"]
    assert cutoff > through
    row = await _candle(pair_id, "1m", BASE)
    assert row["open"] == Decimal("1.00")     # earliest created_at
    assert row["close"] == Decimal("1.30")    # latest created_at
    assert row["high"] == Decimal("1.30")
    assert row["low"] == Decimal("1.00")
    assert row["volume"] == Decimal("6")      # 1 + 2 + sell gold out 3
    assert row["n"] == 3
    assert row["first_id"] < row["last_id"]
    state = await _state(pair_id)
    assert state["history_ready"] is True and state["last_event_id"] == cutoff
    first_version = state["history_version"]

    # Repeated event materialization is exact (no double count) and rotates the generation.
    async with async_session_maker() as db:
        await backfill_fx_history(db, pair_id)
    assert (await _state(pair_id))["last_event_id"] == cutoff
    assert await _candle(pair_id, "1m", BASE) == row
    # Initialize at :00 and trades at :10/:20/:30 occupy four 10s buckets
    # plus 1m/15m/1h -> seven rows; repeat materialization must not add any.
    assert await _candle_count(pair_id) == 7
    second_version = (await _state(pair_id))["history_version"]
    assert second_version != first_version

    # An in-flight flusher batch from the previous generation is rejected.
    async with async_session_maker() as db:
        with pytest.raises(FxCandleCursorConflict):
            async with db.begin():
                await apply_candle_batch(
                    db, pair_id=pair_id, expected_event_id=cutoff,
                    through_event_id=cutoff, rows=[],
                    history_version=first_version, history_ready=None)


@pytest.mark.asyncio
async def test_warm_public_quotes_share_committed_inputs_without_pair_sql(monkeypatch):
    from dataclasses import replace
    from sqlalchemy import event
    from app.core.database import engine
    from app.services import trading_prices
    from app.services.market_read_state import FxReadState

    async with async_session_maker() as db:
        pair = FxPair(currency_code='WARM', currency_name='Warm',
                      gold_reserve=Decimal('100'), foreign_reserve=Decimal('100'))
        db.add(pair)
        await db.commit()
        await db.refresh(pair)
        state = FxReadState.from_pair(pair)
    registry = trading_prices.PricingRegistry(async_session_maker)
    registry.configure(memory_products=frozenset({'fx'}))
    registry.install_committed(state)
    monkeypatch.setattr(trading_prices, 'REGISTRY', registry)
    statements = []
    def observed(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine.sync_engine, 'before_cursor_execute', observed)
    try:
        # Separate requests/users use one committed input; history may query DB.
        quotes, snapshots = [], []
        for user in (11, 22):
            async with async_session_maker() as db:
                quotes.append(await trading.quote(db, state.id, 'buy', Decimal('1'), user))
                snapshots.append(await trading.get_public_snapshot(db, state.id))
        assert quotes[0].output_amount == quotes[1].output_amount
        assert snapshots[0].pair.pool_version == snapshots[1].pair.pool_version == state.pool_version
        assert all('fx_pair.' not in statement.lower() for statement in statements)
        assert not {'gold_reserve', 'buy_fee_rate'} & snapshots[0].pair.model_dump().keys()
        registry.install_committed(replace(state, gold_reserve=Decimal('200'), pool_version=2))
        async with async_session_maker() as db:
            updated = await trading.get_public_snapshot(db, state.id)
        assert updated.price == Decimal('2') and updated.pair.pool_version == 2
    finally:
        event.remove(engine.sync_engine, 'before_cursor_execute', observed)
