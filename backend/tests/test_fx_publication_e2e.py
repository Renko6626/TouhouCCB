"""WP4a focused integration: committed trade -> bounded publisher -> SSE.

Uses the real ``AsyncSession`` plus the real in-process broker on a disposable
SQLite file (DATABASE_URL), so the wrapper's commit/refresh path and the
worker's post-commit frame build are exercised together.
"""
import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlmodel import SQLModel

from app.core.database import async_session_maker, engine
from app.models.audit import AuditEvent
from app.models.base import SiteConfig, User
from app.models.fx import FxCandle, FxMarketDataState, FxPair, FxPoolEvent, FxTrade, FxTreasury, FxWallet
from app.models.title import Title
from app.services import site_config
from app.services.fx import market_data, publisher, trading, liquidity
from app.services.fx.pool_events import append_pool_event
from app.services.fx.market_state import FxMarketDataService
from app.services.realtime import BROKER

_TABLES = [Title.__table__, User.__table__, SiteConfig.__table__, FxPair.__table__,
           FxTreasury.__table__, FxWallet.__table__, FxTrade.__table__,
           FxCandle.__table__, FxMarketDataState.__table__, AuditEvent.__table__, FxPoolEvent.__table__]

_ENGINE_URL = str(engine.url)

# This module drops/creates tables on the application engine, so it refuses to
# run against anything but a disposable SQLite file.  The check must be exact:
# this worktree itself lives under /tmp, so a relative dev path such as
# backend/data/thccb.db would otherwise look "disposable" after resolution.
_DB_FILE = engine.url.database
_DISPOSABLE_SQLITE = (
    _ENGINE_URL.startswith("sqlite")
    and (
        _DB_FILE in (None, "", ":memory:")
        or Path(_DB_FILE).resolve().parent in (Path("/dev/shm"), Path("/tmp"))
    )
)
pytestmark = pytest.mark.skipif(
    not _DISPOSABLE_SQLITE,
    reason="requires a disposable sqlite DATABASE_URL directly under /dev/shm or /tmp",
)


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _dispose_app_engine():
    yield
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _schema():
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: SQLModel.metadata.drop_all(sync_conn, tables=_TABLES))
        await conn.run_sync(lambda sync_conn: SQLModel.metadata.create_all(sync_conn, tables=_TABLES))
    site_config.clear_cache()
    yield


async def _seed():
    suffix = uuid4().hex[:8]
    async with async_session_maker() as db:
        user = User(username=f"fx-{suffix}", cash=Decimal("100"), debt=Decimal("0"),
                    tos_accepted_at=datetime.now(timezone.utc))
        pair = FxPair(currency_code=f"G{suffix}", currency_name="Gold", status="trading",
                      gold_reserve=Decimal("100"), foreign_reserve=Decimal("100"),
                      buy_fee_rate=Decimal("0.01"), sell_fee_rate=Decimal("0.01"))
        db.add_all([user, pair])
        await db.flush()
        db.add(FxTreasury(pair_id=pair.id))
        db.add(SiteConfig(key="fx_enabled", value="true", value_type="bool"))
        audit = AuditEvent(event_type="fx_fund", ref_table="fx_pair", ref_id=pair.id,
                           ts=datetime.now(timezone.utc), payload={"action": "initial_issuance"})
        db.add(audit)
        await db.flush()
        append_pool_event(db, pair_id=pair.id, kind="initialize", created_at=audit.ts,
                          pre_reserves=None, post_reserves=(pair.gold_reserve, pair.foreign_reserve),
                          gold_volume=Decimal("0"), audit_id=audit.id)
        await db.commit()
        return user.id, pair.id


@pytest.mark.asyncio
async def test_trade_response_returns_while_publication_is_still_blocked(monkeypatch):
    uid, pid = await _seed()
    sub, _anchor = await BROKER.subscribe(f"fx:{pid}")
    instance = publisher.FxPublisher()
    monkeypatch.setattr(publisher, "PUBLISHER", instance)

    entered, release = asyncio.Event(), asyncio.Event()
    original = market_data.publish_pair_frame

    async def gated(pair_id, post_price, broker=None):
        entered.set()
        await release.wait()
        await original(pair_id, post_price, broker=broker)

    monkeypatch.setattr(market_data, "publish_pair_frame", gated)
    await instance.start()
    try:
        async with async_session_maker() as db:
            public = await trading.execute_trade(db, uid, pid, "buy", Decimal("10"),
                                                 Decimal("0"), "e2e-1")

        # The trade response returned although the publish hook is blocked.
        await asyncio.wait_for(entered.wait(), 3)
        assert sub.q.qsize() == 0
        # The financial transaction is already committed while publication
        # is still in flight.
        async with async_session_maker() as check:
            row = (await check.execute(
                select(FxTrade).where(FxTrade.id == public.id))).scalars().first()
            assert row is not None and row.input_amount == public.input_amount

        release.set()
        blob = await asyncio.wait_for(sub.q.get(), 3)
        payload = json.loads(blob.decode().split("data: ", 1)[1])
        assert payload["market_id"] == pid
        assert payload["type"] == "fx"
        assert payload["data"]["price"] == str(public.post_price)
        assert Decimal(payload["data"]["volume"]) == public.input_amount
        assert instance.stats()["published"] == 1
    finally:
        release.set()
        await instance.stop()
        await BROKER.unsubscribe(f"fx:{pid}", sub)


@pytest.mark.asyncio
async def test_dropped_frame_still_leaves_reconnect_snapshot_consistent(monkeypatch):
    uid, pid = await _seed()
    stopped = publisher.FxPublisher()  # worker never started: every frame drops
    monkeypatch.setattr(publisher, "PUBLISHER", stopped)

    async with async_session_maker() as db:
        public = await trading.execute_trade(db, uid, pid, "buy", Decimal("10"),
                                             Decimal("0"), "e2e-2")
    assert stopped.stats()["dropped"] == 1

    # Reconnect snapshot (what fx_stream sends on every SSE connect) reflects
    # the committed trade despite the dropped realtime frame.
    async with async_session_maker() as db:
        snapshot = await trading.get_public_snapshot(db, pid)
    assert snapshot.volume_24h == public.input_amount
    assert snapshot.pair.pool_version == 2


@pytest.mark.asyncio
async def test_liquidity_commit_publishes_price_events_and_reconnect_keeps_zero_volume(monkeypatch):
    """Real funding/withdrawal -> existing runtime/publisher -> public SSE + history."""
    from app.services.fx import market_state
    from app.services.fx.market_reads import read_fx_stream_history
    uid, pid = await _seed()
    runtime = FxMarketDataService(async_session_maker)
    monkeypatch.setattr(market_state, "FX_MARKET_DATA", runtime)
    instance = publisher.FxPublisher()
    monkeypatch.setattr(publisher, "PUBLISHER", instance)
    await runtime.start(write_owner=True)
    sub, _ = await BROKER.subscribe(f"fx:{pid}")
    await instance.start()
    try:
        for operation, gold in ((liquidity.fund_pair, "100"), (liquidity.withdraw_pair, "50")):
            async with async_session_maker() as db:
                await operation(db, pid, Decimal(gold), Decimal("0"), uid)
            await instance.drain()
            payload = json.loads((await asyncio.wait_for(sub.q.get(), 3)).decode().split("data: ", 1)[1])["data"]
            assert payload["history_ready"] is True
            assert len(payload["events"]) == 1
            event = payload["events"][0]
            assert set(event) == {"id", "ts", "pre_price", "post_price", "gold_volume", "n_trades"}
            assert Decimal(event["gold_volume"]) == 0 and event["n_trades"] == 0
            assert "trades" not in payload and "history_tail_through_trade_id" not in payload
            assert payload["history_tail_through_event_id"] == event["id"]
        assert Decimal(payload["price"]) == Decimal("1.5")
        async with async_session_maker() as db:
            assert (await db.execute(select(FxTrade).where(FxTrade.pair_id == pid))).scalars().all() == []
            history = await read_fx_stream_history(db, pid, datetime.now(timezone.utc))
        segment = history["history_tail"]["1m"]
        assert max(map(Decimal, segment["h"])) == Decimal("2")
        assert all(Decimal(v) == 0 for v in segment["v"])
        assert sum(segment["events"]) == 3 and sum(segment["trades"]) == 0
        assert history["history_tail_through_event_id"] == event["id"]
    finally:
        await instance.stop()
        await BROKER.unsubscribe(f"fx:{pid}", sub)
        await runtime.stop()

@pytest.mark.asyncio
async def test_controlled_bot_commit_reaches_public_frame(monkeypatch):
    from app.models.bot import BotProfile
    from app.schemas.pve_fx import FxPveConfig, FxPairBotConfig, FxPveGenerateRequest
    from app.services.pve_fx import accounts, executor
    from app.services.pve_fx.config import save_fx_config
    from app.services.pve_fx.types import FxDecision, FxOrderIntent, FxTickFrame
    uid, pid = await _seed()
    async with async_session_maker() as db:
        item = (await accounts.generate_fx_bots(db, FxPveGenerateRequest(pair_id=pid,
            items=[{'template':'random', 'count':1}], initial_cash='100', initial_foreign='0'), uid))[0]
        profile = await db.get(BotProfile, item.profile_id)
        profile.status = 'active'
        await save_fx_config(db, FxPveConfig(enabled=True, pair_configs={pid:FxPairBotConfig(enabled=True)}), uid)
        await db.commit()
    sub, _ = await BROKER.subscribe(f'fx:{pid}')
    instance = publisher.FxPublisher()
    monkeypatch.setattr(publisher, 'PUBLISHER', instance)
    await instance.start()
    try:
        result = await executor.execute_bot_order(item.profile_id,
            FxDecision(FxOrderIntent('buy_foreign', Decimal('.1'), 'test')),
            FxTickFrame(1, pid, datetime.now(timezone.utc), (), Decimal('1')))
        assert result.rejection is None
        await instance.drain()
        payload = json.loads((await asyncio.wait_for(sub.q.get(), 3)).decode().split('data: ', 1)[1])['data']
        assert Decimal(payload['volume']) == result.fill.input_amount
        assert instance.stats()['published'] == 1
        async with async_session_maker() as db:
            event = (await db.execute(select(FxPoolEvent).where(FxPoolEvent.trade_id == result.fill.trade_id))).scalars().one()
            assert event.gold_volume == result.fill.input_amount
    finally:
        await instance.stop()
        await BROKER.unsubscribe(f'fx:{pid}', sub)
