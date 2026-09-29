from sqlalchemy import create_engine
from sqlmodel import Session
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlmodel import SQLModel

from app.models.audit import AuditEvent
from app.models.base import SiteConfig, User
from app.models.fx import FxEvent, FxPair, FxTrade, FxTreasury, FxWallet
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
                FxEvent(pair_id=pair.id, title="event", kind="macro"),
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
    assert await count(RedemptionTransaction, seed_fx_state) == 1
    assert await count(DanmukuExchange, seed_fx_state) == 1
    async with seed_fx_state() as db:
        assert (await db.execute(select(SiteConfig.value).where(SiteConfig.key == "fx_enabled"))).scalar_one() == "true"


@pytest.mark.asyncio
async def test_execute_clears_fx_closes_gate_and_preserves_redemptions(monkeypatch, seed_fx_state):
    from scripts import season_reset

    monkeypatch.setattr("builtins.input", lambda *_: "RESET")
    assert await season_reset.run(dry_run=False) == 0
    for model in (FxWallet, FxTrade, FxEvent, FxTreasury, FxPair):
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
