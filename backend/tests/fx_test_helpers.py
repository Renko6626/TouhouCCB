from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, delete, select
from sqlmodel import Session, SQLModel

from app.models import credit  # noqa: F401 - register LiquidationEvent.run_id target for isolated schema
from app.models.audit import AuditEvent
from app.models.base import SiteConfig, User
from app.models.fx import (
    FxPoolEvent, FxCandle, FxShortPosition, FxMarketDataState, FxPair, FxTrade, FxTreasury, FxWallet,
)
from app.models.title import Title
from app.services import site_config
from app.services.credit.ownership import WriteOwnership


class AsyncCompatSession:
    """Async-shaped adapter over a real synchronous SQLite transaction."""
    def __init__(self, session): self._session = session
    def add(self, value): self._session.add(value)
    def add_all(self, values): self._session.add_all(values)
    async def execute(self, statement, params=None): return self._session.execute(statement, params)
    async def flush(self): self._session.flush()
    async def commit(self): self._session.commit()
    async def rollback(self): self._session.rollback()
    async def refresh(self, value): self._session.refresh(value)
    async def get(self, model, key): return self._session.get(model, key)
    @property
    def sync_session(self): return self._session
    @property
    def info(self): return self._session.info
    def get_bind(self): return self._session.get_bind()
    async def close(self): self._session.close()
    @property
    def new(self): return self._session.new
    @property
    def dirty(self): return self._session.dirty
    @property
    def deleted(self): return self._session.deleted
    def in_transaction(self): return self._session.in_transaction()
    def expire_all(self): self._session.expire_all()

    @asynccontextmanager
    async def begin(self):
        with self._session.begin():
            yield self

    class _AsyncNested:
        def __init__(self, session): self.session = session; self.transaction = None
        async def __aenter__(self):
            self.transaction = self.session.begin_nested()
            self.transaction.__enter__()
            return self
        async def __aexit__(self, exc_type, exc, tb):
            if exc_type:
                self.transaction.__exit__(exc_type, exc, tb)
            else:
                self.transaction.__exit__(None, None, None)

    def begin_nested(self):
        return self._AsyncNested(self._session)

    async def __aenter__(self): return self
    async def __aexit__(self, exc_type, exc, tb):
        if exc_type:
            self._session.rollback()


@pytest_asyncio.fixture
async def fx_db(monkeypatch):
    owner = WriteOwnership(url="sqlite+aiosqlite:///:memory:")
    await owner.acquire()
    for module in ("app.api.v1.admin_fx", "app.services.fx.liquidity", "app.services.fx.trading",
                   "app.services.credit.ownership", "app.services.fx.writer"):
        monkeypatch.setattr(f"{module}.OWNERSHIP", owner)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    tables = [Title.__table__, User.__table__, SiteConfig.__table__, FxPair.__table__,
              FxTreasury.__table__, FxWallet.__table__, FxShortPosition.__table__, FxTrade.__table__,
              FxCandle.__table__, FxMarketDataState.__table__, AuditEvent.__table__]
    SQLModel.metadata.create_all(engine)
    @asynccontextmanager
    async def writer_session_maker():
        with Session(engine, expire_on_commit=False) as writer_raw:
            yield AsyncCompatSession(writer_raw)
    monkeypatch.setattr('app.core.database.async_session_maker', writer_session_maker)
    try:
        with Session(engine, expire_on_commit=False) as raw:
            db = AsyncCompatSession(raw)
            db.add_all([
                SiteConfig(key="fx_enabled", value="true", value_type="bool"),
            ])
            await db.commit()
            site_config.clear_cache()
            yield db
    finally:
        engine.dispose()
        await owner.release()


async def add_pair(db, *, code="TST", gold="100", foreign="100", initial="1"):
    pair = FxPair(currency_code=code, currency_name="Test", status="trading",
                  gold_reserve=Decimal(gold), foreign_reserve=Decimal(foreign),
                  initial_price=Decimal(initial))
    db.add(pair)
    await db.flush()
    treasury = FxTreasury(pair_id=pair.id, gold_balance=Decimal(gold), foreign_balance=Decimal(foreign))
    db.add(treasury)
    await db.commit()
    return pair, treasury


async def backfill_fx_history(db, pair_id: int) -> str:
    """Materialise a ready FX history generation from committed pool events.

    Isolated test databases have no lifespan runtime. Aggregate committed pool
    events with the production candle layer and persist their coverage cursor.
    Returns the new history version.
    """
    from app.services.fx.candles import compute_fx_candle_rows, new_history_version

    events = (await db.execute(
        select(FxPoolEvent).where(FxPoolEvent.pair_id == int(pair_id)).order_by(FxPoolEvent.id)
    )).scalars().all()
    rows = compute_fx_candle_rows(events)
    await db.execute(delete(FxCandle).where(FxCandle.pair_id == int(pair_id)))
    version = new_history_version()
    for row in rows:
        db.add(FxCandle(**row))
    state = (await db.execute(
        select(FxMarketDataState).where(FxMarketDataState.pair_id == int(pair_id))
    )).scalars().first()
    if state is None:
        state = FxMarketDataState(pair_id=int(pair_id), history_version=version)
        db.add(state)
    state.history_version = version
    state.last_event_id = int(events[-1].id) if events else 0
    state.history_ready = True
    await db.commit()
    return version


@pytest.fixture
def committed_fx_registry(monkeypatch):
    """Isolate the shared projection while exercising real commit owners."""
    from app.services import trading_prices
    registry = trading_prices.PricingRegistry()
    registry.configure(memory_products=frozenset({'fx'}))
    monkeypatch.setattr(trading_prices, 'REGISTRY', registry)
    return registry


async def assert_committed_fx(registry, db, pair_id):
    from app.services.market_read_state import FxReadState
    pair = await db.get(FxPair, pair_id)
    await db.refresh(pair)
    actual = registry.peek_fx(pair_id)
    expected = FxReadState.from_pair(pair)
    assert actual is not None
    from dataclasses import asdict
    from app.services.trading_prices import clock_string
    # SQLite returns naive UTC timestamps after refresh; published clocks use UTC.
    def values(state):
        return {key: clock_string(value) if isinstance(value, datetime) else value
                for key, value in asdict(state).items()}
    assert values(actual) == values(expected), (actual, expected)
