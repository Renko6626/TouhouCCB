from sqlalchemy import create_engine
from sqlmodel import Session
from decimal import Decimal
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlmodel import SQLModel

from app.models.audit import AuditEvent
from app.models.base import SiteConfig, User
from app.models.fx import (
    FxCandle, FxMarketDataState, FxPair, FxTrade, FxTreasury, FxWallet, FxShortPosition,
)
from app.models.redemption import DanmukuExchange, RedemptionTransaction
from app.services import audit_service


class AsyncCompatSession:
    def __init__(self, raw): self.raw = raw
    def add(self, value): self.raw.add(value)
    def add_all(self, values): self.raw.add_all(values)
    async def execute(self, statement): return self.raw.execute(statement)
    async def flush(self): self.raw.flush()
    async def get(self, model, key): return self.raw.get(model, key)
    def in_transaction(self): return self.raw.in_transaction()
    class _Begin:
        def __init__(self, raw): self.raw = raw
        async def __aenter__(self): self.tx = self.raw.begin(); self.tx.__enter__(); return self
        async def __aexit__(self, typ, value, tb): self.tx.__exit__(typ, value, tb)
    def begin(self): return self._Begin(self.raw)
    async def __aenter__(self): return self
    async def __aexit__(self, typ, value, tb):
        self.raw.rollback()
        self.raw.close()


@pytest_asyncio.fixture(autouse=True)
async def seed_fx_state(monkeypatch):
    from scripts import season_reset

    local_engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    from sqlalchemy import event
    @event.listens_for(local_engine, "connect")
    def enable_foreign_keys(conn, _):
        conn.execute("PRAGMA foreign_keys=ON")
    SQLModel.metadata.create_all(local_engine)
    def local_sessions(): return AsyncCompatSession(Session(local_engine))
    monkeypatch.setattr(season_reset, "async_session_maker", local_sessions)
    async with local_sessions() as db:
        async with db.begin():
            db.add(SiteConfig(key="initial_balance", value="500", value_type="decimal"))
            db.add(SiteConfig(key="fx_enabled", value="true", value_type="bool"))
            user = User(username="fx-user", casdoor_id="fx-user", cash=Decimal("12"))
            db.add(user)
            await db.flush()
            pair = FxPair(currency_code="FXT", currency_name="Test FX", status="trading",
                          gold_reserve=Decimal("100"), foreign_reserve=Decimal("100"))
            db.add(pair)
            await db.flush()
            db.add_all([
                FxWallet(user_id=user.id, pair_id=pair.id, foreign_amount=Decimal("2"), cost_basis=Decimal("2")),
                FxTreasury(pair_id=pair.id, gold_balance=Decimal("3"), foreign_balance=Decimal("4")),
                FxTrade(pair_id=pair.id, user_id=user.id, side="buy", input_amount=Decimal("1"),
                        output_amount=Decimal("1"), pre_gold_reserve=Decimal("100"),
                        pre_foreign_reserve=Decimal("100"), post_gold_reserve=Decimal("101"),
                        post_foreign_reserve=Decimal("99"), post_price=Decimal("1.02")),
                FxCandle(pair_id=pair.id, interval="1m",
                         bucket_start=datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc),
                         open_price=Decimal("1.02"), high_price=Decimal("1.02"),
                         low_price=Decimal("1.02"), close_price=Decimal("1.02"),
                         gold_volume=Decimal("1"), n_trades=1,
                         first_trade_at=datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc),
                         first_trade_id=1,
                         last_trade_at=datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc),
                         last_trade_id=1),
                FxMarketDataState(pair_id=pair.id, last_trade_id=1, history_ready=True),
                RedemptionTransaction(user_id=user.id, amount=Decimal("5"), batch_name_snapshot="kept"),
                DanmukuExchange(user_id=user.id, qq_user_id="1", room_id="r", yuan=Decimal("1"),
                                huo=Decimal("1"), amount=Decimal("2"), code_string="kept-code"),
            ])
            audit_service.record(db, "fx_trade", user_id=user.id, ref_table="fx_pair", ref_id=pair.id,
                                 payload={"pair_id": pair.id})
            audit_service.record(db, "redeem_purchase", user_id=user.id,
                                 payload={"amount": "5"}, user_after={"cash": "7", "debt": "0"})


    yield local_sessions
    local_engine.dispose()


async def count(model, sessions):
    async with sessions() as db:
        return int((await db.execute(select(func.count()).select_from(model))).scalar_one())


@pytest.mark.asyncio
async def test_dry_run_leaves_fx_gate_rows_and_redemption_records(monkeypatch, seed_fx_state):
    from scripts import season_reset

    assert await season_reset.run(dry_run=True) == 0
    assert await count(FxPair, seed_fx_state) == 1
    assert await count(FxWallet, seed_fx_state) == 1
    assert await count(FxCandle, seed_fx_state) == 1
    assert await count(FxMarketDataState, seed_fx_state) == 1
    assert await count(RedemptionTransaction, seed_fx_state) == 1
    assert await count(DanmukuExchange, seed_fx_state) == 1
    async with seed_fx_state() as db:
        assert (await db.execute(select(SiteConfig.value).where(SiteConfig.key == "fx_enabled"))).scalar_one() == "true"


@pytest.mark.asyncio
async def test_execute_clears_fx_closes_gate_and_preserves_redemptions(monkeypatch, seed_fx_state):
    from scripts import season_reset

    monkeypatch.setattr("builtins.input", lambda *_: "RESET")
    assert await season_reset.run(dry_run=False) == 0
    for model in (FxCandle, FxMarketDataState, FxWallet, FxTrade, FxTreasury, FxPair):
        assert await count(model, seed_fx_state) == 0
    assert await count(RedemptionTransaction, seed_fx_state) == 1
    assert await count(DanmukuExchange, seed_fx_state) == 1
    async with seed_fx_state() as db:
        assert (await db.execute(select(SiteConfig.value).where(SiteConfig.key == "fx_enabled"))).scalar_one() == "false"
        events = (await db.execute(select(AuditEvent))).scalars().all()
        assert all(not e.event_type.startswith("fx_") for e in events)
        assert any(e.event_type == "redeem_purchase" for e in events)


@pytest.mark.asyncio
async def test_failed_verification_rolls_back_fx_and_gate(monkeypatch, seed_fx_state):
    from scripts import season_reset

    original = season_reset.audit_service.record

    def corrupt(*args, **kwargs):
        event = original(*args, **kwargs)
        if event.event_type == "user_register":
            event.user_after = {**(event.user_after or {}), "cash": "9999"}
        return event

    monkeypatch.setattr(season_reset.audit_service, "record", corrupt)
    monkeypatch.setattr("builtins.input", lambda *_: "RESET")
    assert await season_reset.run(dry_run=False) == 2
    assert await count(FxPair, seed_fx_state) == 1
    async with seed_fx_state() as db:
        assert (await db.execute(select(SiteConfig.value).where(SiteConfig.key == "fx_enabled"))).scalar_one() == "true"
    assert await count(RedemptionTransaction, seed_fx_state) == 1


@pytest.mark.asyncio
async def test_fx_identity_sequence_is_not_reset(monkeypatch, seed_fx_state):
    from scripts import season_reset

    async with seed_fx_state() as db:
        old_id = (await db.execute(select(FxPair.id))).scalar_one()
    monkeypatch.setattr("builtins.input", lambda *_: "RESET")
    assert await season_reset.run(dry_run=False) == 0
    async with seed_fx_state() as db:
        async with db.begin():
            pair = FxPair(currency_code="FXN", currency_name="Next FX")
            db.add(pair)
            await db.flush()
            new_id = pair.id
    # SQLite integer rowids may reuse deleted IDs; production PostgreSQL preserves sequences.
    assert new_id >= old_id


@pytest.mark.asyncio
async def test_reset_live_short_clears_obligation_and_short_audit(monkeypatch, seed_fx_state, capsys):
    from datetime import datetime, timezone
    from scripts import season_reset
    async with seed_fx_state() as db:
        async with db.begin():
            user = (await db.execute(select(User))).scalar_one()
            pair = (await db.execute(select(FxPair))).scalar_one()
            db.add(FxShortPosition(user_id=user.id, pair_id=pair.id,
                principal_foreign=Decimal("2"), interest_foreign=Decimal("1"),
                restricted_gold=Decimal("8"), interest_last_accrued_at=datetime.now(timezone.utc)))
            db.add(SiteConfig(key="fx_short_enabled", value="true", value_type="bool"))
            audit_service.record(db, "admin_fx_short_writeoff", user_id=user.id, payload={"pair_id": pair.id})
            audit_service.record(db, "interest_accrual", user_id=user.id, ref_table="fx_short_position", payload={"currency": "foreign", "pair_id": pair.id})
    assert await season_reset.run(dry_run=True) == 0
    assert "fx_short_position" in capsys.readouterr().out
    assert await count(FxShortPosition, seed_fx_state) == 1
    # The self-check is inside the transaction: corruption must restore the
    # live liability, pool and both opening gates, then a clean retry succeeds.
    original = season_reset.audit_service.record
    def corrupt(*args, **kwargs):
        event = original(*args, **kwargs)
        if event.event_type == "user_register":
            event.user_after = {**event.user_after, "cash": "9999"}
        return event
    monkeypatch.setattr("builtins.input", lambda *_: "RESET")
    monkeypatch.setattr(season_reset.audit_service, "record", corrupt)
    assert await season_reset.run(dry_run=False) == 2
    assert await count(FxShortPosition, seed_fx_state) == 1
    assert await count(FxPair, seed_fx_state) == 1
    async with seed_fx_state() as db:
        assert (await db.execute(select(SiteConfig.value).where(SiteConfig.key == "fx_short_enabled"))).scalar_one() == "true"
        assert (await db.execute(select(User.cash))).scalar_one() == Decimal("12")
    monkeypatch.setattr(season_reset.audit_service, "record", original)
    assert await season_reset.run(dry_run=False) == 0
    assert await count(FxShortPosition, seed_fx_state) == 0
    async with seed_fx_state() as db:
        assert (await db.execute(select(SiteConfig.value).where(SiteConfig.key == "fx_short_enabled"))).scalar_one() == "false"
        user = (await db.execute(select(User))).scalar_one()
        assert user.cash == Decimal("500") and user.debt == 0 and not user.credit_frozen
        events = (await db.execute(select(AuditEvent))).scalars().all()
        assert any(e.event_type == "redeem_purchase" for e in events)
        assert all(e.event_type != "admin_fx_short_writeoff" and not e.event_type.startswith("fx_") for e in events)
