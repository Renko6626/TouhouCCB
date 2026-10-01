"""Task 3 integration: real commit hooks, rebuild generation and season cleanup.

Asserts only observable behaviour on the real async database:

* a real player commit through the request wrapper reaches the incremental
  runtime (proved by a long reconcile interval: only the post-commit hint can
  wake the consumer in time), and replay / rejection / in-session rollback
  leave the derived candles untouched;
* a rebuild reproduces a fresh full aggregation exactly, rotates the history
  generation and honours the frozen source cutoff for paused/archived pairs.

The storage layer and the runtime ring are covered by their own suites; this
file only covers the wiring between them.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.database import async_session_maker
from app.models.base import SiteConfig, User
from app.models.fx import FxCandle, FxMarketDataState, FxPair, FxTrade, FxTreasury
from app.services import site_config
from app.services.credit import flags as credit_flags
from app.services.fx import trading
from app.services.fx.candles import FxCandleCursorConflict, apply_candle_batch
from app.services.fx.market_state import FX_MARKET_DATA
from scripts.backfill_fx_candles import rebuild_pair

UTC = timezone.utc
BASE = datetime(2026, 10, 1, 9, 0, 0, tzinfo=UTC)


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
                     archived: bool = False) -> int:
    async with async_session_maker() as db:
        pair = FxPair(currency_code=code, currency_name="Integration", status=status,
                      archived=archived, gold_reserve=Decimal("1000"),
                      foreign_reserve=Decimal("1000"), buy_fee_rate=Decimal("0"),
                      sell_fee_rate=Decimal("0"))
        db.add(pair)
        await db.flush()
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
                        pre_gold_reserve=Decimal("1000"),
                        pre_foreign_reserve=Decimal("1000"),
                        post_gold_reserve=Decimal("1000"),
                        post_foreign_reserve=Decimal("1000"),
                        post_price=Decimal(price), created_at=at, source="player")
        db.add(trade)
        await db.flush()
        trade_id = int(trade.id)
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
            "first_id": int(row.first_trade_id), "last_id": int(row.last_trade_id),
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
        return {"last_trade_id": int(row.last_trade_id),
                "history_version": str(row.history_version),
                "history_ready": bool(row.history_ready)}


async def _wait_applied(pair_id: int, trade_id: int, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        state = FX_MARKET_DATA.state(pair_id)
        if state is not None and state["applied_trade_id"] >= trade_id:
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
        assert rows[0]["open"] == rows[0]["close"] == Decimal(public.post_price)

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


# ── rebuild ───────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_rebuild_pair_matches_fresh_aggregation_and_rotates_generation():
    pair_id = await _seed_pair(code="RB")
    await _add_raw_trade(pair_id, at=BASE + timedelta(seconds=30), price="1.30", input_amount="1")
    await _add_raw_trade(pair_id, at=BASE + timedelta(seconds=10), price="1.10", input_amount="2")
    await _add_raw_trade(pair_id, at=BASE + timedelta(seconds=20), price="1.20",
                         side="sell", input_amount="1", output_amount="3")
    through = await _max_trade_id(pair_id)

    async with async_session_maker() as db:
        async with db.begin():
            cutoff = await rebuild_pair(db, pair_id, through)
    assert cutoff == through
    row = await _candle(pair_id, "1m", BASE)
    assert row["open"] == Decimal("1.10")     # earliest created_at
    assert row["close"] == Decimal("1.30")    # latest created_at
    assert row["high"] == Decimal("1.30")
    assert row["low"] == Decimal("1.10")
    assert row["volume"] == Decimal("6")      # 1 + 2 + sell gold out 3
    assert row["n"] == 3
    assert (row["first_id"], row["last_id"]) == (2, 1)
    state = await _state(pair_id)
    assert state["history_ready"] is True and state["last_trade_id"] == through
    first_version = state["history_version"]

    # Repeated rebuild is exact (no double count) and rotates the generation.
    async with async_session_maker() as db:
        async with db.begin():
            assert await rebuild_pair(db, pair_id, through) == through
    assert await _candle(pair_id, "1m", BASE) == row
    # 3 trades at :10/:20/:30 occupy three 10s buckets plus one bucket each for
    # 1m/15m/1h -> exactly 6 rows; a repeat rebuild must not add any.
    assert await _candle_count(pair_id) == 6
    second_version = (await _state(pair_id))["history_version"]
    assert second_version != first_version

    # An in-flight flusher batch from the previous generation is rejected.
    async with async_session_maker() as db:
        with pytest.raises(FxCandleCursorConflict):
            async with db.begin():
                await apply_candle_batch(
                    db, pair_id=pair_id, expected_trade_id=through,
                    through_trade_id=through, rows=[],
                    history_version=first_version, history_ready=None)


@pytest.mark.asyncio
async def test_rebuild_honours_cutoff_for_paused_archived_pair():
    pair_id = await _seed_pair(code="RB2", status="paused", archived=True)
    first = await _add_raw_trade(pair_id, at=BASE + timedelta(seconds=10),
                                 price="1.10", input_amount="1")
    await _add_raw_trade(pair_id, at=BASE + timedelta(seconds=20),
                         price="1.20", input_amount="1")

    async with async_session_maker() as db:
        async with db.begin():
            cutoff = await rebuild_pair(db, pair_id, first)
    assert cutoff == first
    row = await _candle(pair_id, "1m", BASE)
    assert row["n"] == 1
    assert row["volume"] == Decimal("1")
    assert row["close"] == Decimal("1.10")
    assert (await _state(pair_id))["last_trade_id"] == first


@pytest.mark.asyncio
async def test_rebuild_pair_refuses_while_owner_runtime_is_started():
    pair_id = await _seed_pair(code="RB3")
    await _add_raw_trade(pair_id, at=BASE, price="1.0", input_amount="1")
    await FX_MARKET_DATA.start(write_owner=True)
    try:
        async with async_session_maker() as db:
            with pytest.raises(RuntimeError, match="offline maintenance"):
                async with db.begin():
                    await rebuild_pair(db, pair_id, await _max_trade_id(pair_id))
    finally:
        await FX_MARKET_DATA.stop()
