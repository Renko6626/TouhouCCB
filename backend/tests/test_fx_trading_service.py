"""Runnable FX service tests; independent of the application lifespan fixture."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy import create_engine
from sqlmodel import SQLModel, Session

from app.models.audit import AuditEvent
from app.models.base import SiteConfig, User
from app.models.fx import FxShortPosition, FxPair, FxTrade, FxTreasury, FxWallet
from app.models.title import Title
from app.services import site_config
from app.services.fx import trading


class AsyncCompatSession:
    """Async-shaped adapter over isolated synchronous SQLite for deterministic tests.

    aiosqlite is unavailable in this sandbox (its worker thread blocks before the
    first query); the service itself still executes through real SQLAlchemy rows.
    """
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


@pytest_asyncio.fixture(scope="module")
async def isolated_db(tmp_path_factory):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    tables = [Title.__table__, User.__table__, SiteConfig.__table__, FxPair.__table__,
              FxTreasury.__table__, FxWallet.__table__, FxShortPosition.__table__, FxTrade.__table__, AuditEvent.__table__]
    SQLModel.metadata.create_all(engine, tables=tables)
    with Session(engine) as raw:
        session = AsyncCompatSession(raw)
        session.add(SiteConfig(key="fx_enabled", value="true", value_type="bool"))
        await session.commit()
    site_config.clear_cache()
    yield engine
    engine.dispose()


@pytest_asyncio.fixture
async def db_session(isolated_db, monkeypatch):
    with Session(isolated_db) as raw:
        session = AsyncCompatSession(raw)
        @asynccontextmanager
        async def isolated_session_maker():
            yield session
        monkeypatch.setattr("app.core.database.async_session_maker", isolated_session_maker)
        yield session


async def seed(db, *, cash="100", debt="0", bot=False, tos=True, status="trading"):
    suffix = uuid4().hex[:8]
    user = User(username=f"fx-{suffix}", cash=Decimal(cash), debt=Decimal(debt),
                is_bot=bot, tos_accepted_at=(datetime.now(timezone.utc) if tos else None))
    pair = FxPair(currency_code=f"G{suffix}", currency_name="Gold", status=status,
                  gold_reserve=Decimal("100"), foreign_reserve=Decimal("100"),
                  buy_fee_rate=Decimal("0.01"), sell_fee_rate=Decimal("0.01"))
    db.add_all([user, pair])
    await db.flush()
    db.add(FxTreasury(pair_id=pair.id))
    await db.commit()
    return user.id, pair.id


@pytest.mark.asyncio
async def test_buy_sell_update_balances_and_audit(db_session):
    uid, pid = await seed(db_session)
    bought = await trading.execute_trade(db_session, uid, pid, "buy", Decimal("10"), Decimal("0"), "buy-1")
    wallet = (await db_session.execute(select(FxWallet).where(FxWallet.user_id == uid))).scalar_one()
    treasury = (await db_session.execute(select(FxTreasury).where(FxTreasury.pair_id == pid))).scalar_one()
    assert bought.output_amount > 0 and wallet.foreign_amount == bought.output_amount
    assert treasury.gold_balance > 0
    sold = await trading.execute_trade(db_session, uid, pid, "sell", bought.output_amount,
                                       Decimal("0"), "sell-1")
    assert sold.output_amount > 0 and wallet.updated_at is not None
    event = (await db_session.execute(select(AuditEvent).where(AuditEvent.event_type == "fx_trade"))).scalars().all()
    assert len(event) == 2
    assert event[-1].payload["wallet_after"]["foreign_amount"] == "0.000000"
    assert "treasury_after" in event[-1].payload


@pytest.mark.asyncio
async def test_stale_min_out_is_409_and_idempotency_replay_is_exact(db_session):
    uid, pid = await seed(db_session)
    q = await trading.quote(db_session, pid, "buy", Decimal("10"), uid)
    with pytest.raises(HTTPException) as exc:
        await trading.execute_trade(db_session, uid, pid, "buy", Decimal("10"), q.output_amount + 1, "stale")
    assert exc.value.status_code == 409
    trade = await trading.execute_trade(db_session, uid, pid, "buy", Decimal("10"), q.output_amount, "idem")
    replay = await trading.execute_trade(db_session, uid, pid, "buy", Decimal("10"), q.output_amount, "idem")
    assert replay.id == trade.id
    with pytest.raises(HTTPException) as mismatch:
        await trading.execute_trade(db_session, uid, pid, "buy", Decimal("10"), q.output_amount - 1, "idem")
    assert mismatch.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"bot": True}, {"tos": False}, {"debt": "1"}, {"status": "paused"}])
async def test_guards_reject_buy(db_session, kwargs):
    uid, pid = await seed(db_session, **kwargs)
    with pytest.raises(HTTPException) as exc:
        await trading.execute_trade(db_session, uid, pid, "buy", Decimal("1"), Decimal("0"), uuid4().hex)
    assert exc.value.status_code == 403
    await db_session.commit()
    assert (await db_session.execute(select(FxWallet).where(FxWallet.user_id == uid))).scalars().first() is None


@pytest.mark.asyncio
async def test_decimal_precision_rejects_seven_fractional_digits_and_accepts_trailing_zeros(db_session):
    uid, pid = await seed(db_session)
    for amount, minimum in ((Decimal("1.0000001"), Decimal("0")),
                            (Decimal("1"), Decimal("0.0000001"))):
        with pytest.raises(trading.TradeRejected):
            await trading.execute_trade(db_session, uid, pid, "buy", amount, minimum, uuid4().hex)
    quote = await trading.quote(db_session, pid, "buy", Decimal("1.000000"), uid)
    assert quote.input_amount == Decimal("1.000000")


@pytest.mark.asyncio
async def test_no_overdraft_when_requests_exceed_cash(db_session):
    uid, pid = await seed(db_session, cash="5")
    await trading.execute_trade(db_session, uid, pid, "buy", Decimal("4"), Decimal("0"), "first")
    with pytest.raises(HTTPException) as exc:
        await trading.execute_trade(db_session, uid, pid, "buy", Decimal("4"), Decimal("0"), "second")
    assert exc.value.status_code == 400
    assert (await db_session.get(User, uid)).cash >= 0


@pytest.mark.asyncio
async def test_snapshot_excludes_trades_older_than_24_hours(db_session):
    uid, pid = await seed(db_session)
    current = await trading.execute_trade(db_session, uid, pid, "buy", Decimal("1"), Decimal("0"), "current")
    db_session.add(FxTrade(pair_id=pid, user_id=uid, side="buy", input_amount=Decimal("3"), output_amount=Decimal("1"),
                           min_out=Decimal("0"), pre_gold_reserve=Decimal("100"), pre_foreign_reserve=Decimal("100"),
                           post_gold_reserve=Decimal("101"), post_foreign_reserve=Decimal("99"), post_price=Decimal("1"),
                           created_at=trading.utcnow() - timedelta(hours=25), idempotency_key="old"))
    await db_session.commit()
    snapshot = await trading.get_public_snapshot(db_session, pid)
    assert snapshot.volume_24h == current.input_amount


# ── I1 snapshot pricing: gold-per-foreign bid/ask with non-negative spread ──

async def _add_pair(db, *, code, gold, foreign, buy_fee="0", sell_fee="0"):
    suffix = uuid4().hex[:6]
    pair = FxPair(currency_code=f"{code}{suffix}", currency_name="Test", status="trading",
                  gold_reserve=Decimal(gold), foreign_reserve=Decimal(foreign),
                  buy_fee_rate=Decimal(buy_fee), sell_fee_rate=Decimal(sell_fee))
    db.add(pair)
    await db.commit()
    return pair.id


@pytest.mark.asyncio
async def test_snapshot_prices_are_gold_per_foreign_with_positive_spread(db_session):
    pid = await _add_pair(db_session, code="BAL", gold="100", foreign="100",
                          buy_fee="0.01", sell_fee="0.01")
    buy = await trading.quote(db_session, pid, "buy", Decimal("1"))
    sell = await trading.quote(db_session, pid, "sell", Decimal("1"))
    q8 = Decimal("0.00000001")
    snapshot = await trading.get_public_snapshot(db_session, pid)

    # buy_price = gold in / foreign out (ask); sell_price = gold out / foreign in (bid).
    assert snapshot.buy_price == (buy.input_amount / buy.output_amount).quantize(q8)
    assert snapshot.sell_price == (sell.output_amount / sell.input_amount).quantize(q8)
    assert snapshot.buy_price > snapshot.price > snapshot.sell_price
    assert snapshot.spread == snapshot.buy_price - snapshot.sell_price
    assert snapshot.spread > 0


@pytest.mark.asyncio
async def test_snapshot_spread_is_non_negative_on_imbalanced_zero_fee_pool(db_session):
    # Zero fees: the spread is pure AMM curvature and must still be >= 0.
    pid = await _add_pair(db_session, code="IMB", gold="300", foreign="100")
    buy = await trading.quote(db_session, pid, "buy", Decimal("1"))
    sell = await trading.quote(db_session, pid, "sell", Decimal("1"))
    q8 = Decimal("0.00000001")
    snapshot = await trading.get_public_snapshot(db_session, pid)
    assert snapshot.buy_price == (buy.input_amount / buy.output_amount).quantize(q8)
    assert snapshot.sell_price == (sell.output_amount / sell.input_amount).quantize(q8)
    assert snapshot.spread == snapshot.buy_price - snapshot.sell_price
    assert snapshot.spread > 0
