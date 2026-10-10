"""WP4a focused tests: FX transaction boundary, parity and lock order.

Runs standalone (``pytest --noconftest``) on isolated in-memory SQLite, using
an async-shaped adapter over a real synchronous SQLAlchemy transaction so the
commit/rollback boundary of ``execute_trade`` / ``execute_trade_in_session``
is observable.
"""
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import create_engine, event as sa_event, select
from sqlalchemy.orm import Session as SASession
from sqlmodel import SQLModel, Session as ModelSession

from app.models.audit import AuditEvent
from app.models.base import SiteConfig, User
from app.models.fx import FxCandle, FxMarketDataState, FxShortPosition, FxPair, FxTrade, FxTreasury, FxWallet
from app.models.title import Title
from app.services.credit.gates import GATES
from app.services.credit.keys import GroupKey
from app.services import site_config
from app.services.fx import publisher, trading


class AsyncCompatSession:
    """Async-shaped adapter over isolated synchronous SQLite for deterministic tests."""

    def __init__(self, session):
        self._session = session

    def add(self, value): self._session.add(value)
    def add_all(self, values): self._session.add_all(values)
    async def execute(self, statement): return self._session.execute(statement)
    async def flush(self): self._session.flush()
    async def commit(self): self._session.commit()
    async def rollback(self): self._session.rollback()
    async def refresh(self, value): self._session.refresh(value)
    async def get(self, model, key): return self._session.get(model, key)
    def in_transaction(self): return self._session.in_transaction()


@pytest_asyncio.fixture
async def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    tables = [Title.__table__, User.__table__, SiteConfig.__table__, FxPair.__table__,
              FxTreasury.__table__, FxWallet.__table__, FxShortPosition.__table__, FxTrade.__table__,
              FxCandle.__table__, FxMarketDataState.__table__, AuditEvent.__table__]
    SQLModel.metadata.create_all(engine)
    with ModelSession(engine) as raw:
        session = AsyncCompatSession(raw)
        session.add(SiteConfig(key="fx_enabled", value="true", value_type="bool"))
        await session.commit()
        site_config.clear_cache()
        yield session
    engine.dispose()


async def seed(db, *, cash="100", debt="0", bot=False, tos=True, status="trading",
               reduce_only=False):
    suffix = uuid4().hex[:8]
    user = User(username=f"fx-{suffix}", cash=Decimal(cash), debt=Decimal(debt),
                is_bot=bot, tos_accepted_at=(datetime.now(timezone.utc) if tos else None))
    pair = FxPair(currency_code=f"G{suffix}", currency_name="Gold", status=status,
                  reduce_only=reduce_only,
                  gold_reserve=Decimal("100"), foreign_reserve=Decimal("100"),
                  buy_fee_rate=Decimal("0.01"), sell_fee_rate=Decimal("0.01"))
    db.add_all([user, pair])
    await db.flush()
    db.add(FxTreasury(pair_id=pair.id))
    await db.commit()
    return user.id, pair.id


def _trades(db):
    return (db._session.execute(select(FxTrade))).scalars().all()


@contextmanager
def record_lock_order():
    """Record ``SELECT ... FOR UPDATE`` targets in execution order."""
    order: list[str | None] = []

    def _record(orm_execute_state):
        stmt = orm_execute_state.statement
        if getattr(stmt, "_for_update_arg", None) is None:
            return
        description = stmt.column_descriptions[0]
        order.append(getattr(description.get("entity"), "__tablename__", None))

    sa_event.listen(SASession, "do_orm_execute", _record)
    try:
        yield order
    finally:
        sa_event.remove(SASession, "do_orm_execute", _record)


# ── transaction boundary ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_in_session_execution_does_not_commit_and_rollback_reverts_all(db_session):
    uid, pid = await seed(db_session)
    before_cash = (await db_session.get(User, uid)).cash

    async with GATES.hold(exclusive=[GroupKey("fx", pid), GroupKey("fx", pid + 10_000)]):
        execution = await trading.execute_trade_in_session(
            db_session, uid, pid, "buy", Decimal("10"), Decimal("0"), "tx-1")

        assert execution.replay is False
        assert execution.public.id == execution.trade.id
        assert execution.public.post_price == execution.trade.post_price
        # The caller's transaction is still open: nothing was committed.
        assert db_session.in_transaction() is True
        assert (await db_session.get(User, uid)).cash < before_cash

        await db_session.rollback()

        user = await db_session.get(User, uid)
        pair = await db_session.get(FxPair, pid)
        treasury = (await db_session.execute(select(FxTreasury))).scalars().first()
        assert user.cash == before_cash
        assert _trades(db_session) == []
        assert (await db_session.execute(select(FxWallet))).scalars().all() == []
        assert (await db_session.execute(select(AuditEvent))).scalars().all() == []
        assert pair.pool_version == 1
        assert pair.gold_reserve == Decimal("100") and pair.foreign_reserve == Decimal("100")
        assert treasury.gold_balance == Decimal("0") and treasury.foreign_balance == Decimal("0")


@pytest.mark.asyncio
async def test_wrapper_commits_so_later_rollback_cannot_undo(db_session):
    uid, pid = await seed(db_session)

    public = await trading.execute_trade(
        db_session, uid, pid, "buy", Decimal("10"), Decimal("0"), "wrap-1")

    await db_session.rollback()  # request session teardown must not undo the trade

    assert (await db_session.execute(
        select(FxTrade).where(FxTrade.id == public.id))).scalars().first() is not None
    assert (await db_session.get(User, uid)).cash == Decimal("100") - public.input_amount


@pytest.mark.asyncio
async def test_wrapper_replay_rolls_back_and_returns_materialized_public(db_session):
    uid, pid = await seed(db_session)
    first = await trading.execute_trade(db_session, uid, pid, "buy", Decimal("10"),
                                        Decimal("0"), "replay-1")

    replay = await trading.execute_trade(db_session, uid, pid, "buy", Decimal("10"),
                                         Decimal("0"), "replay-1")

    assert replay.id == first.id
    assert replay.input_amount == first.input_amount
    assert replay.post_price == first.post_price
    # Legacy contract: a replay releases the pair/user locks instead of
    # leaving the request transaction open.
    assert db_session.in_transaction() is False
    assert len(_trades(db_session)) == 1


@pytest.mark.asyncio
async def test_in_session_replay_reports_replay_and_leaves_transaction_open(db_session):
    uid, pid = await seed(db_session)
    async with GATES.hold(exclusive=[GroupKey("fx", pid), GroupKey("fx", pid + 10_000)]):
        first = await trading.execute_trade_in_session(
            db_session, uid, pid, "buy", Decimal("10"), Decimal("0"), "same-key")
        assert first.replay is False
        await db_session.commit()  # caller-owned boundary

        second = await trading.execute_trade_in_session(
            db_session, uid, pid, "buy", Decimal("10"), Decimal("0"), "same-key")

        assert second.replay is True
        assert second.trade.id == first.trade.id
        assert second.public.id == first.public.id
        # In-session never rolls back: the caller still owns the transaction.
        assert db_session.in_transaction() is True
        assert len(_trades(db_session)) == 1
        await db_session.rollback()


@pytest.mark.asyncio
async def test_player_cannot_reserve_liquidation_idempotency_key(db_session):
    uid, pid = await seed(db_session)
    async with GATES.hold(exclusive=[GroupKey("fx", pid), GroupKey("fx", pid + 10_000)]):
        with pytest.raises(trading.TradeRejected, match="reserved"):
            await trading.execute_trade_in_session(
                db_session, uid, pid, "buy", Decimal("1"), Decimal("0"), "liq:7:3")
        assert _trades(db_session) == []


@pytest.mark.asyncio
async def test_in_session_idempotency_mismatch_is_409_and_mutates_nothing(db_session):
    uid, pid = await seed(db_session)
    await trading.execute_trade(db_session, uid, pid, "buy", Decimal("10"), Decimal("0"), "k-1")

    async with GATES.hold(exclusive=[GroupKey("fx", pid), GroupKey("fx", pid + 10_000)]):
        with pytest.raises(HTTPException) as exc:
            await trading.execute_trade_in_session(db_session, uid, pid, "buy", Decimal("10"),
                                                   Decimal("1"), "k-1")

        assert exc.value.status_code == 409
        await db_session.rollback()
        assert len(_trades(db_session)) == 1


@pytest.mark.asyncio
async def test_in_session_error_status_codes_and_no_partial_mutation(db_session):
    uid, pid = await seed(db_session, cash="5")

    async with GATES.hold(exclusive=[GroupKey("fx", pid), GroupKey("fx", pid + 10_000)]):
        with pytest.raises(HTTPException) as missing:
            await trading.execute_trade_in_session(db_session, uid, pid + 10_000, "buy",
                                                   Decimal("1"), Decimal("0"), uuid4().hex)
        assert missing.value.status_code == 404
        await db_session.rollback()

        with pytest.raises(HTTPException) as cash_exc:
            await trading.execute_trade_in_session(db_session, uid, pid, "buy", Decimal("10"),
                                                   Decimal("0"), uuid4().hex)
        assert cash_exc.value.status_code == 400
        await db_session.rollback()

        with pytest.raises(HTTPException) as wallet_exc:
            await trading.execute_trade_in_session(db_session, uid, pid, "sell", Decimal("1"),
                                                   Decimal("0"), uuid4().hex)
        assert wallet_exc.value.status_code == 400
        await db_session.rollback()

        quote = await trading.quote(db_session, pid, "buy", Decimal("1"), uid)
        with pytest.raises(HTTPException) as min_out:
            await trading.execute_trade_in_session(db_session, uid, pid, "buy", Decimal("1"),
                                                   quote.output_amount + 1, uuid4().hex)
        assert min_out.value.status_code == 409
        await db_session.rollback()

        with pytest.raises(trading.TradeRejected):
            await trading.execute_trade_in_session(db_session, uid, pid, "hold", Decimal("1"),
                                                   Decimal("0"), uuid4().hex)
        await db_session.rollback()

        assert _trades(db_session) == []
        assert (await db_session.execute(select(FxWallet))).scalars().all() == []
        assert (await db_session.get(User, uid)).cash == Decimal("5")


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs,status", [
    ({"bot": True}, 403),
    ({"tos": False}, 403),
    ({"status": "paused"}, 403),
])
async def test_guard_status_codes_match_between_wrapper_and_in_session(db_session, kwargs, status):
    uid, pid = await seed(db_session, **kwargs)
    for call in (trading.execute_trade, trading.execute_trade_in_session):
        gate = (GATES.hold(exclusive=[GroupKey("fx", pid)])
                if call is trading.execute_trade_in_session else nullcontext())
        async with gate:
            with pytest.raises(HTTPException) as exc:
                await call(db_session, uid, pid, "buy", Decimal("1"), Decimal("0"), uuid4().hex)
            assert exc.value.status_code == status
            await db_session.rollback()
    assert _trades(db_session) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status,reduce_only,side,allowed", [
    ("trading", False, "buy", True),
    ("trading", False, "sell", True),
    ("trading", True, "buy", False),
    ("trading", True, "sell", True),
    ("paused", True, "buy", False),
    ("paused", True, "sell", True),
    ("paused", False, "buy", False),
    ("paused", False, "sell", False),
    ("draft", True, "sell", False),
    ("closed", True, "sell", False),
])
async def test_f9_player_side_matrix(db_session, status, reduce_only, side, allowed):
    """F9 double-axis product state for player orders (spec §9.1)."""
    uid, pid = await seed(db_session, status=status, reduce_only=reduce_only)
    if side == "sell":
        db_session.add(FxWallet(user_id=uid, pair_id=pid, foreign_amount=Decimal("1"),
                                cost_basis=Decimal("1")))
        await db_session.commit()

    async with GATES.hold(exclusive=[GroupKey("fx", pid), GroupKey("fx", pid + 10_000)]):
        if allowed:
            execution = await trading.execute_trade_in_session(
                db_session, uid, pid, side, Decimal("1"), Decimal("0"), uuid4().hex)
            assert execution.public.side == side
        else:
            with pytest.raises(HTTPException) as exc:
                await trading.execute_trade_in_session(
                    db_session, uid, pid, side, Decimal("1"), Decimal("0"), uuid4().hex)
            assert exc.value.status_code == 403
        await db_session.rollback()


@pytest.mark.asyncio
async def test_reduce_only_buy_rejection_detail_is_actionable(db_session):
    uid, pid = await seed(db_session, status="trading", reduce_only=True)
    async with GATES.hold(exclusive=[GroupKey("fx", pid), GroupKey("fx", pid + 10_000)]):
        with pytest.raises(HTTPException) as exc:
            await trading.execute_trade_in_session(db_session, uid, pid, "buy", Decimal("1"),
                                                   Decimal("0"), uuid4().hex)
        assert exc.value.status_code == 403
        assert exc.value.detail == "FX pair is reduce-only"
        await db_session.rollback()


@pytest.mark.asyncio
async def test_paused_without_reduce_only_keeps_legacy_detail(db_session):
    uid, pid = await seed(db_session, status="paused", reduce_only=False)
    async with GATES.hold(exclusive=[GroupKey("fx", pid), GroupKey("fx", pid + 10_000)]):
        with pytest.raises(HTTPException) as exc:
            await trading.execute_trade_in_session(db_session, uid, pid, "sell", Decimal("1"),
                                                   Decimal("0"), uuid4().hex)
        assert exc.value.status_code == 403
        assert exc.value.detail == "FX pair is not trading"
        await db_session.rollback()


@pytest.mark.asyncio
async def test_disabled_fx_returns_403_without_commit(db_session):
    uid, pid = await seed(db_session)
    row = (await db_session.execute(
        select(SiteConfig).where(SiteConfig.key == "fx_enabled"))).scalars().first()
    row.value = "false"
    await db_session.commit()
    site_config.clear_cache()

    async with GATES.hold(exclusive=[GroupKey("fx", pid), GroupKey("fx", pid + 10_000)]):
        with pytest.raises(HTTPException) as exc:
            await trading.execute_trade_in_session(db_session, uid, pid, "buy", Decimal("1"),
                                                   Decimal("0"), uuid4().hex)
        assert exc.value.status_code == 403
        await db_session.rollback()
        assert _trades(db_session) == []


@pytest.mark.asyncio
async def test_lock_order_is_pair_user_wallet_treasury(db_session):
    uid, pid = await seed(db_session)
    async with GATES.hold(exclusive=[GroupKey("fx", pid), GroupKey("fx", pid + 10_000)]):
        with record_lock_order() as order:
            await trading.execute_trade_in_session(db_session, uid, pid, "buy", Decimal("10"),
                                                   Decimal("0"), "lock-1")
        assert order == ["fx_pair", "user", "fx_wallet", "fx_treasury"]
        await db_session.rollback()


@pytest.mark.asyncio
async def test_sell_path_keeps_lock_order_and_conserves_value(db_session):
    uid, pid = await seed(db_session)
    bought = await trading.execute_trade(db_session, uid, pid, "buy", Decimal("10"),
                                         Decimal("0"), "buy-1")
    async with GATES.hold(exclusive=[GroupKey("fx", pid), GroupKey("fx", pid + 10_000)]):
        with record_lock_order() as order:
            sold = await trading.execute_trade_in_session(
                db_session, uid, pid, "sell", bought.output_amount, Decimal("0"), "sell-1")
        assert order == ["fx_pair", "user", "fx_wallet", "fx_treasury"]
        assert sold.public.side == "sell"
        assert sold.public.output_amount > 0
        await db_session.commit()


# ── post-commit publication is off the response path ────────────────────────

@pytest.mark.asyncio
async def test_wrapper_does_not_wait_for_publication(db_session, monkeypatch):
    uid, pid = await seed(db_session)
    published = []

    async def hook(publication):
        published.append((publication.pair_id, publication.post_price,
                          publication.trade_id))

    instance = publisher.FxPublisher(publish=hook)
    monkeypatch.setattr(publisher, "PUBLISHER", instance)
    await instance.start()
    try:
        public = await trading.execute_trade(db_session, uid, pid, "buy", Decimal("10"),
                                             Decimal("0"), "pub-1")
        # The response never waits for the public frame.
        assert published == []
        await instance.drain(2)
        assert published == [(pid, public.post_price, public.id)]
        assert instance.stats()["published"] == 1
    finally:
        await instance.stop()


@pytest.mark.asyncio
async def test_dropped_publication_cannot_lose_the_committed_trade(db_session, monkeypatch):
    uid, pid = await seed(db_session)
    stopped = publisher.FxPublisher()  # bounded worker intentionally never started
    monkeypatch.setattr(publisher, "PUBLISHER", stopped)

    public = await trading.execute_trade(db_session, uid, pid, "buy", Decimal("10"),
                                         Decimal("0"), "pub-2")

    assert stopped.stats()["dropped"] == 1
    await db_session.rollback()
    assert (await db_session.execute(
        select(FxTrade).where(FxTrade.id == public.id))).scalars().first() is not None
    # Reconnect snapshot reflects the committed trade even though the frame
    # was dropped: SSE clients read a fresh snapshot on connect.
    snapshot = await trading.get_public_snapshot(db_session, pid)
    assert snapshot.volume_24h == public.input_amount


@pytest.mark.asyncio
async def test_publish_public_event_retains_signature_and_only_enqueues(db_session, monkeypatch):
    uid, pid = await seed(db_session)
    accepted = []

    def enqueue(pair_id, post_price, *, trade_id=None):
        accepted.append((pair_id, post_price, trade_id))
        return True

    monkeypatch.setattr(publisher, "enqueue_publication", enqueue)
    trade = FxTrade(pair_id=pid, user_id=uid, side="buy", input_amount=Decimal("1"),
                    output_amount=Decimal("1"), min_out=Decimal("0"),
                    pre_gold_reserve=Decimal("100"), pre_foreign_reserve=Decimal("100"),
                    post_gold_reserve=Decimal("101"), post_foreign_reserve=Decimal("99"),
                    post_price=Decimal("1"), idempotency_key="event-1")
    db_session.add(trade)
    await db_session.flush()

    await trading.publish_public_event(trade)

    assert accepted == [(pid, Decimal("1"), trade.id)]
    await db_session.rollback()
