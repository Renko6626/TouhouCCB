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
from app.models.fx import FxPair, FxTrade, FxTreasury, FxWallet
from app.models.title import Title
from app.services import site_config
from app.services.fx import market_data, publisher, trading
from app.services.realtime import BROKER

_TABLES = [Title.__table__, User.__table__, SiteConfig.__table__, FxPair.__table__,
           FxTreasury.__table__, FxWallet.__table__, FxTrade.__table__, AuditEvent.__table__]

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
        await db.commit()
        return user.id, pair.id


@pytest.mark.asyncio
async def test_trade_response_returns_while_publication_is_still_blocked(monkeypatch):
    uid, pid = await _seed()
    sub, _anchor = await BROKER.subscribe(pid)
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
        await BROKER.unsubscribe(pid, sub)


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
