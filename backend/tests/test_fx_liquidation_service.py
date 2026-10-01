"""WP4b focused tests: in-session FX forced-liquidation sell executor.

Runs standalone (``pytest --noconftest``) on isolated in-memory SQLite with an
async-shaped adapter over a real synchronous transaction, so conservation,
idempotency, F9 product semantics and the "no commit / no publish" boundary are
observable.  ``fx_enabled`` is seeded **false** on purpose: liquidation is a
system action and must not depend on the player trading gate.
"""
from contextlib import contextmanager
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
from app.models.fx import FxCandle, FxMarketDataState, FxPair, FxTrade, FxTreasury, FxWallet
from app.models.title import Title
from app.services import site_config
from app.services.credit.fx_quote import FxPairSnapshot, quote_fx_group
from app.services.fx import publisher, trading
from app.services.fx.amm import quote_sell


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
              FxTreasury.__table__, FxWallet.__table__, FxTrade.__table__,
              FxCandle.__table__, FxMarketDataState.__table__, AuditEvent.__table__]
    SQLModel.metadata.create_all(engine, tables=tables)
    with ModelSession(engine) as raw:
        session = AsyncCompatSession(raw)
        # Player trading stays disabled for every test in this module.
        session.add(SiteConfig(key="fx_enabled", value="false", value_type="bool"))
        await session.commit()
        site_config.clear_cache()
        yield session
    engine.dispose()


async def seed(db, *, cash="0", debt="100", bot=False, tos=True, status="trading",
               reduce_only=False, gold="100", foreign="100", sell_fee="0.01",
               wallet=None, cost_basis="0", treasury_gold="0", treasury_foreign="0"):
    suffix = uuid4().hex[:8]
    user = User(username=f"liq-{suffix}", cash=Decimal(cash), debt=Decimal(debt),
                is_bot=bot, tos_accepted_at=(datetime.now(timezone.utc) if tos else None))
    pair = FxPair(currency_code=f"L{suffix}", currency_name="Gold", status=status,
                  reduce_only=reduce_only,
                  gold_reserve=Decimal(gold), foreign_reserve=Decimal(foreign),
                  buy_fee_rate=Decimal("0.01"), sell_fee_rate=Decimal(sell_fee))
    db.add_all([user, pair])
    await db.flush()
    db.add(FxTreasury(pair_id=pair.id, gold_balance=Decimal(treasury_gold),
                      foreign_balance=Decimal(treasury_foreign)))
    if wallet is not None:
        db.add(FxWallet(user_id=user.id, pair_id=pair.id,
                        foreign_amount=Decimal(wallet), cost_basis=Decimal(cost_basis)))
    await db.commit()
    return user.id, pair.id


async def _execute(db, uid, pid, *, run_id=7, round_no=3, mode="full", partial_pct=None):
    return await trading.execute_liquidation_sell_in_session(
        db, user_id=uid, pair_id=pid, run_id=run_id, round_no=round_no,
        mode=mode, partial_pct=partial_pct)


def _trades(db):
    return (db._session.execute(select(FxTrade))).scalars().all()


def _wallet(db, user_id=None):
    stmt = select(FxWallet)
    if user_id is not None:
        stmt = stmt.where(FxWallet.user_id == user_id)
    return (db._session.execute(stmt)).scalars().first()


def _treasury(db):
    return (db._session.execute(select(FxTreasury))).scalars().first()


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


# ── execution + conservation ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_full_liquidation_moves_both_currencies_and_stays_uncommitted(db_session):
    uid, pid = await seed(db_session, cash="0", debt="100", wallet="10", cost_basis="5",
                          treasury_gold="5", treasury_foreign="0")
    expected = quote_sell(Decimal("10"), Decimal("100"), Decimal("100"), Decimal("0.01"))

    execution = await _execute(db_session, uid, pid, run_id=7, round_no=3)

    assert execution.blocked_reason is None and execution.replay is False
    assert execution.quote is not None
    trade = execution.trade
    assert trade is not None and trade.id is not None
    assert execution.public.id == trade.id
    assert trade.source == "liquidation"
    assert trade.side == "sell"
    assert trade.idempotency_key == "liq:7:3"
    assert trade.min_out == Decimal("0")          # no stale user min_out can block
    assert trade.input_amount == expected.input_amount == Decimal("10.000000")
    assert trade.output_amount == expected.output_amount
    assert trade.fee_amount == expected.fee_amount == Decimal("0.100000")
    assert trade.post_gold_reserve == expected.post_gold_reserve
    assert trade.post_foreign_reserve == expected.post_foreign_reserve
    assert trade.post_price == expected.post_price

    wallet, pair, treasury, user = (_wallet(db_session), await db_session.get(FxPair, pid),
                                    _treasury(db_session), await db_session.get(User, uid))
    assert wallet.foreign_amount == Decimal("0")
    assert wallet.cost_basis == Decimal("0")
    assert user.cash == expected.output_amount
    assert pair.pool_version == 2
    assert pair.gold_reserve == expected.post_gold_reserve
    assert pair.foreign_reserve == expected.post_foreign_reserve
    assert treasury.foreign_balance == expected.fee_amount
    assert treasury.gold_balance == Decimal("5")

    # Foreign: pool + treasury + user wallet conserved.
    assert (pair.foreign_reserve + treasury.foreign_balance + wallet.foreign_amount
            == Decimal("110"))
    # Gold: pool + treasury + user cash conserved (fee is charged in foreign).
    assert pair.gold_reserve + treasury.gold_balance + user.cash == Decimal("105")

    audit = (await db_session.execute(select(AuditEvent).where(
        AuditEvent.event_type == "fx_trade"))).scalars().all()
    assert len(audit) == 1
    assert audit[0].payload["source"] == "liquidation"
    assert Decimal(audit[0].payload["wallet_after"]["foreign_amount"]) == Decimal("0")
    assert audit[0].payload["pool_version"] == 2

    # Not committed: the whole batch disappears on the caller's rollback.
    assert db_session.in_transaction() is True
    await db_session.rollback()
    assert _trades(db_session) == []
    wallet, pair, treasury, user = (_wallet(db_session), await db_session.get(FxPair, pid),
                                    _treasury(db_session), await db_session.get(User, uid))
    assert wallet.foreign_amount == Decimal("10") and wallet.cost_basis == Decimal("5")
    assert user.cash == Decimal("0")
    assert pair.pool_version == 1 and pair.gold_reserve == Decimal("100")
    assert treasury.foreign_balance == Decimal("0") and treasury.gold_balance == Decimal("5")
    assert (await db_session.execute(select(AuditEvent))).scalars().all() == []


@pytest.mark.asyncio
async def test_partial_liquidation_scales_cost_basis_proportionally(db_session):
    uid, pid = await seed(db_session, wallet="10", cost_basis="4")
    expected = quote_sell(Decimal("1"), Decimal("100"), Decimal("100"), Decimal("0.01"))

    execution = await _execute(db_session, uid, pid, mode="partial",
                               partial_pct=Decimal("0.10"))

    assert execution.trade.input_amount == expected.input_amount == Decimal("1.000000")
    wallet = _wallet(db_session)
    assert wallet.foreign_amount == Decimal("9.000000")
    # 4 - 4 * 1 / 10 = 3.6 (same formula as the player sell path).
    assert wallet.cost_basis == Decimal("3.6")
    await db_session.rollback()


@pytest.mark.asyncio
async def test_partial_batch_ceils_to_unit_and_caps_at_balance(db_session):
    uid, pid = await seed(db_session, wallet="1.234567")
    execution = await _execute(db_session, uid, pid, mode="partial",
                               partial_pct=Decimal("0.10"))
    assert execution.trade.input_amount == Decimal("0.123457")  # ceil of 0.1234567
    await db_session.rollback()

    # A high-price pool (100 gold / 1 foreign) so the smallest sellable unit
    # still yields a positive gold output; the ceil'd batch is capped at the
    # whole wallet instead of exceeding it.
    uid2, pid2 = await seed(db_session, gold="100", foreign="1",
                            wallet="0.000001", sell_fee="0")
    execution = await _execute(db_session, uid2, pid2, mode="partial",
                               partial_pct=Decimal("0.10"))
    # ceil(0.0000001) = 0.000001 and it is capped at the whole balance.
    assert execution.trade.input_amount == Decimal("0.000001")
    assert _wallet(db_session, uid2).foreign_amount == Decimal("0")
    await db_session.rollback()


@pytest.mark.asyncio
async def test_liquidation_is_requoted_under_the_lock(db_session):
    uid, pid = await seed(db_session, wallet="10")
    first = await _execute(db_session, uid, pid, run_id=1, round_no=1, mode="partial",
                           partial_pct=Decimal("0.5"))
    await db_session.commit()

    second = await _execute(db_session, uid, pid, run_id=1, round_no=2, mode="partial",
                            partial_pct=Decimal("0.5"))

    assert first.trade.input_amount == Decimal("5.000000")
    assert second.trade.input_amount == Decimal("2.500000")
    # The second batch starts from the reserves left by the first, not from a
    # caller-side stale quote.
    assert second.trade.pre_gold_reserve == first.trade.post_gold_reserve
    assert second.trade.pre_foreign_reserve == first.trade.post_foreign_reserve
    await db_session.rollback()


@pytest.mark.asyncio
async def test_liquidation_ignores_player_gates(db_session):
    # fx_enabled=false (fixture), bot account, no TOS, no debt: forced
    # liquidation is a system action and must still execute.
    uid, pid = await seed(db_session, bot=True, tos=False, debt="0", wallet="1")
    execution = await _execute(db_session, uid, pid)
    assert execution.trade is not None
    assert execution.trade.source == "liquidation"
    await db_session.rollback()


@pytest.mark.asyncio
async def test_cash_priority_repayment_conservation_in_one_transaction(db_session):
    """The caller's debt repayment keeps assets-minus-liability invariant."""
    uid, pid = await seed(db_session, cash="0", debt="100", wallet="10", cost_basis="5",
                          treasury_gold="5", treasury_foreign="0")
    user_before = await db_session.get(User, uid)
    pair_before = await db_session.get(FxPair, pid)
    treasury_before = _treasury(db_session)
    gold_total_before = pair_before.gold_reserve + treasury_before.gold_balance + user_before.cash
    foreign_total_before = (pair_before.foreign_reserve + treasury_before.foreign_balance
                            + _wallet(db_session).foreign_amount)
    net_before = gold_total_before - user_before.debt

    execution = await _execute(db_session, uid, pid)
    # Orchestration step that WP7 performs in the same transaction.
    user = await db_session.get(User, uid)
    repaid = min(user.cash, user.debt)
    user.cash -= repaid
    user.debt -= repaid
    await db_session.commit()

    assert repaid > 0 and execution.trade.output_amount == repaid
    pair_after = await db_session.get(FxPair, pid)
    treasury_after = _treasury(db_session)
    wallet_after = _wallet(db_session)
    gold_total_after = pair_after.gold_reserve + treasury_after.gold_balance + user.cash
    foreign_total_after = (pair_after.foreign_reserve + treasury_after.foreign_balance
                           + wallet_after.foreign_amount)
    assert foreign_total_after == foreign_total_before      # pool + treasury + wallet
    assert gold_total_after - user.debt == net_before       # includes the liability
    assert user.debt == Decimal("100") - execution.trade.output_amount
    assert user.cash == Decimal("0")


# ── idempotency by run/round ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_same_run_round_replays_without_a_second_sale(db_session):
    uid, pid = await seed(db_session, wallet="10", cost_basis="5")
    first = await _execute(db_session, uid, pid, run_id=4, round_no=2)
    await db_session.commit()
    pool_after_first = (await db_session.get(FxPair, pid)).gold_reserve
    cash_after_first = (await db_session.get(User, uid)).cash

    replay = await _execute(db_session, uid, pid, run_id=4, round_no=2)

    assert replay.replay is True
    assert replay.blocked_reason is None
    assert replay.quote is None
    assert replay.trade.id == first.trade.id
    assert replay.public.id == first.public.id
    assert replay.public.input_amount == first.public.input_amount
    assert len(_trades(db_session)) == 1
    assert _wallet(db_session).foreign_amount == Decimal("0")
    assert (await db_session.get(FxPair, pid)).gold_reserve == pool_after_first
    assert (await db_session.get(User, uid)).cash == cash_after_first
    await db_session.rollback()


@pytest.mark.asyncio
async def test_same_key_for_a_different_pair_is_a_conflict(db_session):
    uid, pid = await seed(db_session, wallet="10")
    await _execute(db_session, uid, pid, run_id=9, round_no=1)
    await db_session.commit()

    other = FxPair(currency_code=f"X{uuid4().hex[:8]}", currency_name="Other",
                   status="trading", gold_reserve=Decimal("100"),
                   foreign_reserve=Decimal("100"), buy_fee_rate=Decimal("0.01"),
                   sell_fee_rate=Decimal("0.01"))
    db_session.add(other)
    await db_session.flush()
    db_session.add(FxTreasury(pair_id=other.id))
    db_session.add(FxWallet(user_id=uid, pair_id=other.id, foreign_amount=Decimal("1"),
                            cost_basis=Decimal("1")))
    await db_session.commit()

    with pytest.raises(HTTPException) as exc:
        await _execute(db_session, uid, other.id, run_id=9, round_no=1)
    assert exc.value.status_code == 409
    await db_session.rollback()
    assert len(_trades(db_session)) == 1


@pytest.mark.asyncio
async def test_liquidation_idempotency_key_format_and_validation():
    assert trading.liquidation_idempotency_key(12, 3) == "liq:12:3"
    assert trading.LIQUIDATION_SOURCE == "liquidation"
    for run_id, round_no in ((0, 1), (1, 0), (-1, 1), (1, -1), ("1", 1), (1, None), (True, 1)):
        with pytest.raises(ValueError):
            trading.liquidation_idempotency_key(run_id, round_no)  # type: ignore[arg-type]


# ── F9 product-state matrix ─────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("status,reduce_only", [
    ("trading", False),
    ("trading", True),
    ("paused", True),
])
async def test_liquidation_executes_only_in_reducible_states(db_session, status, reduce_only):
    uid, pid = await seed(db_session, status=status, reduce_only=reduce_only, wallet="1")
    execution = await _execute(db_session, uid, pid)
    assert execution.blocked_reason is None and execution.trade is not None
    assert execution.trade.source == "liquidation"
    assert _wallet(db_session).foreign_amount == Decimal("0")
    await db_session.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,reduce_only,reason", [
    ("paused", False, "pair_paused"),
    ("draft", False, "pair_draft"),
    ("closed", True, "pair_closed"),
])
async def test_liquidation_blocks_fully_halted_states_without_mutation(
        db_session, status, reduce_only, reason):
    uid, pid = await seed(db_session, status=status, reduce_only=reduce_only,
                          wallet="10", cost_basis="5", treasury_foreign="2")

    execution = await _execute(db_session, uid, pid)

    assert execution.trade is None and execution.public is None
    assert execution.replay is False
    assert execution.blocked_reason == reason
    assert execution.quote.blocked_reason == reason
    assert execution.quote.foreign_in == Decimal("0")
    assert execution.idempotency_key == "liq:7:3"
    await db_session.rollback()

    pair, treasury, user = (await db_session.get(FxPair, pid), _treasury(db_session),
                            await db_session.get(User, uid))
    assert _wallet(db_session).foreign_amount == Decimal("10")
    assert pair.pool_version == 1 and pair.gold_reserve == Decimal("100")
    assert treasury.foreign_balance == Decimal("2")
    assert user.cash == Decimal("0")
    assert _trades(db_session) == []


@pytest.mark.asyncio
async def test_nothing_to_sell_blocks_without_creating_a_wallet(db_session):
    uid, pid = await seed(db_session, wallet=None)
    execution = await _execute(db_session, uid, pid)
    assert execution.blocked_reason == "nothing_to_sell"
    assert execution.trade is None
    await db_session.rollback()
    assert (await db_session.execute(select(FxWallet))).scalars().all() == []

    uid2, pid2 = await seed(db_session, wallet="0")
    execution = await _execute(db_session, uid2, pid2)
    assert execution.blocked_reason == "nothing_to_sell"
    await db_session.rollback()


@pytest.mark.asyncio
async def test_unquotable_batch_blocks_instead_of_falling_back_to_full(db_session):
    # One satoshi of gold reserve makes a one-satoshi sale round to zero output;
    # the batch must block, not silently sell the whole wallet.
    uid, pid = await seed(db_session, gold="0.000001", foreign="100", sell_fee="0",
                          wallet="0.000001", cost_basis="0.000001")
    execution = await _execute(db_session, uid, pid, mode="full")
    assert execution.blocked_reason == "quote_failed"
    assert execution.trade is None
    await db_session.rollback()
    assert _wallet(db_session).foreign_amount == Decimal("0.000001")
    assert (await db_session.get(FxPair, pid)).pool_version == 1
    assert _trades(db_session) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,partial_pct", [
    ("partial", None),
    ("partial", Decimal("0")),
    ("partial", Decimal("1.5")),
    ("half", Decimal("0.5")),
])
async def test_invalid_batch_parameters_are_rejected_before_any_db_work(
        db_session, mode, partial_pct):
    # Bogus pair/user ids prove validation happens before locking/quoting.
    with pytest.raises(ValueError):
        await trading.execute_liquidation_sell_in_session(
            db_session, user_id=10_000_000, pair_id=10_000_000, run_id=1, round_no=1,
            mode=mode, partial_pct=partial_pct)


# ── lock order, purity, publication ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_lock_order_is_pair_user_wallet_treasury(db_session):
    uid, pid = await seed(db_session, wallet="10")
    with record_lock_order() as order:
        await _execute(db_session, uid, pid)
    assert order == ["fx_pair", "user", "fx_wallet", "fx_treasury"]
    await db_session.rollback()


@pytest.mark.asyncio
async def test_blocked_liquidation_never_locks_the_treasury(db_session):
    uid, pid = await seed(db_session, status="paused", reduce_only=False, wallet="10")
    with record_lock_order() as order:
        execution = await _execute(db_session, uid, pid)
    assert execution.blocked_reason == "pair_paused"
    assert order == ["fx_pair", "user", "fx_wallet"]
    await db_session.rollback()


@pytest.mark.asyncio
async def test_liquidation_never_publishes_and_never_commits(db_session, monkeypatch):
    uid, pid = await seed(db_session, wallet="10")
    enqueued = []

    def enqueue(*args, **kwargs):
        enqueued.append((args, kwargs))
        return True

    monkeypatch.setattr(publisher, "enqueue_publication", enqueue)

    execution = await _execute(db_session, uid, pid)

    assert enqueued == []
    assert execution.trade is not None
    assert db_session.in_transaction() is True
    await db_session.rollback()
    assert _trades(db_session) == []


@pytest.mark.asyncio
async def test_quote_matches_the_accepted_credit_quote_helper(db_session):
    uid, pid = await seed(db_session, wallet="3.5", sell_fee="0.02")
    pair = await db_session.get(FxPair, pid)
    expected = quote_fx_group(
        FxPairSnapshot(pair_id=pid, status=pair.status, reduce_only=pair.reduce_only,
                       gold_reserve=pair.gold_reserve, foreign_reserve=pair.foreign_reserve,
                       sell_fee_rate=pair.sell_fee_rate),
        foreign_amount=Decimal("3.5"), mode="full", partial_pct=Decimal("1"))

    execution = await _execute(db_session, uid, pid)

    assert execution.quote == expected
    assert execution.trade.input_amount == expected.foreign_in
    assert execution.trade.output_amount == expected.gold_out
    assert execution.trade.fee_amount == expected.fee_foreign
    await db_session.rollback()
