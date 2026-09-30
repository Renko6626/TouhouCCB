"""Task 3a: atomic FX short-open ledger persisted economic scenarios.

These scenarios run against the real SQLite test database and the real AMM /
risk / audit services.  They assert persisted cash, pool, treasury, debt,
lock, trade and audit postconditions -- never source text, constants or private
helper structure.  The caller owns the transaction and the complete GATE set in
every case, exactly like the later player API.

A regression here means a short open can create or destroy real gold/foreign
stock, charge newly borrowed principal for time before T, leave the account
below the shared initial margin, mutate state on a rejected request, or book a
second borrow on a retried idempotency key.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.database import async_session_maker
from app.models.audit import AuditEvent
from app.models.base import SiteConfig, User
from app.models.fx import FxPair, FxShortPosition, FxTrade, FxTreasury, FxWallet
from app.services import audit_replay, site_config
from app.services.credit import flags as credit_flags
from app.services.credit.flags import CreditFlags
from app.services.credit.gates import GATES
from app.services.credit.keys import GroupKey
from app.services.credit.risk import discover_dependencies
from app.services.fx import trading
from app.services.fx.quantize import amount_down
from app.services.fx.shorts import (
    ShortCoverRejected,
    ShortRetryCredit,
    execute_short_cover_in_session,
    execute_short_open_in_session,
)

pytestmark = pytest.mark.asyncio

ZERO = D("0")
UNIFIED = CreditFlags(
    unified_credit_enabled=True,
    credit_leverage=D("4"),
    credit_maintenance_ratio=D("0.1"),
)


@pytest.fixture(autouse=True)
def _credit_flags():
    credit_flags.clear_flags()
    credit_flags.set_flags(UNIFIED)
    site_config.clear_cache()
    yield
    credit_flags.clear_flags()
    credit_flags.set_new_risk_frozen(None)
    site_config.clear_cache()


def _utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _short_row(user_id, pair_id, *, principal="10", interest="0", restricted="0",
               basis="0", accrued_ago_sec=0):
    total = D(principal) + D(interest)
    clock = (
        datetime.now(timezone.utc) - timedelta(seconds=accrued_ago_sec)
        if total > 0 else None
    )
    return FxShortPosition(
        user_id=user_id, pair_id=pair_id,
        principal_foreign=D(principal), interest_foreign=D(interest),
        restricted_gold=D(restricted), proceeds_basis_gold=D(basis),
        interest_last_accrued_at=clock,
    )


async def _seed(*, cash="1000", debt="0", debt_ago_sec=None, rate="0",
                treasury_foreign="100000",
                gold="1000", treasury_gold=None, foreign="1000",
                buy_fee="0", sell_fee="0",
                limit="100000", status="trading", reduce_only=False,
                archived=False, wallet_foreign=None, short=None,
                short_enabled=True, loan_enabled=True, fx_enabled=True,
                credit_frozen=False):
    async with async_session_maker() as db:
        debt_clock = (
            datetime.now(timezone.utc) - timedelta(seconds=debt_ago_sec)
            if debt_ago_sec is not None and D(debt) > 0 else None
        )
        user = User(username=uuid4().hex, cash=D(cash), debt=D(debt),
                    debt_last_accrued_at=debt_clock,
                    credit_frozen=credit_frozen,
                    tos_accepted_at=datetime.now(timezone.utc))
        db.add(user)
        await db.flush()
        pair = FxPair(
            currency_code=uuid4().hex[:16], currency_name="T",
            status=status, reduce_only=reduce_only, archived=archived,
            gold_reserve=D(gold), foreign_reserve=D(foreign),
            buy_fee_rate=D(buy_fee), sell_fee_rate=D(sell_fee),
            short_lending_limit_foreign=D(limit),
        )
        db.add(pair)
        await db.flush()
        db.add(FxTreasury(pair_id=pair.id,
                          gold_balance=D(treasury_gold) if treasury_gold is not None else D(gold),
                          foreign_balance=D(treasury_foreign)))
        configs = [
            ("fx_enabled", "true" if fx_enabled else "false", "bool"),
            ("loan_enabled", "true" if loan_enabled else "false", "bool"),
            ("loan_daily_rate", rate, "decimal"),
        ]
        if short_enabled:
            configs.append(("fx_short_enabled", "true", "bool"))
        for key, value, vtype in configs:
            db.add(SiteConfig(key=key, value=value, value_type=vtype))
        if wallet_foreign is not None:
            db.add(FxWallet(user_id=user.id, pair_id=pair.id,
                            foreign_amount=D(wallet_foreign), cost_basis=ZERO))
        if short is not None:
            db.add(_short_row(user.id, pair.id, **short))
        await db.commit()
        site_config.clear_cache()
        return int(user.id), int(pair.id)


async def _open(uid, pid, q, *, min_out="0", key=None, attempts=5):
    """One caller-owned transaction and complete gate set, as the API will do."""
    key = key or uuid4().hex
    for _ in range(attempts):
        async with async_session_maker() as db:
            target = GroupKey("fx", pid)
            deps = await discover_dependencies(db, uid, extra_groups=[target])
            if db.in_transaction():
                await db.rollback()
            async with GATES.hold(
                exclusive=[target],
                shared=[g for g in deps.groups if g != target],
            ):
                try:
                    execution = await execute_short_open_in_session(
                        db, user_id=uid, pair_id=pid, foreign_amount=D(q),
                        min_gold_out=D(min_out), idempotency_key=key,
                        credit_deps=deps,
                    )
                    await db.commit()
                    return execution
                except ShortRetryCredit:
                    await db.rollback()
                    continue
                except BaseException:
                    await db.rollback()
                    raise
    raise AssertionError("short-open retries exhausted")


async def _cover(uid, pid, *, q=None, cover_all=False, max_gold_in="1000000",
                 key=None, attempts=5):
    """One caller-owned cover transaction and complete gate set.

    Mirrors the API contract: the caller discovers dependencies, rolls the
    discovery transaction back, holds the target exclusive gate plus every other
    dependency shared, then commits once.  Rejections roll back and re-raise so a
    scenario can assert that no money/debt/lock moved.
    """
    key = key or uuid4().hex
    for _ in range(attempts):
        async with async_session_maker() as db:
            target = GroupKey("fx", pid)
            deps = await discover_dependencies(db, uid, extra_groups=[target])
            if db.in_transaction():
                await db.rollback()
            async with GATES.hold(
                exclusive=[target],
                shared=[g for g in deps.groups if g != target],
            ):
                try:
                    execution = await execute_short_cover_in_session(
                        db, user_id=uid, pair_id=pid,
                        foreign_amount=None if q is None else D(q),
                        cover_all=cover_all, max_gold_in=D(max_gold_in),
                        idempotency_key=key, credit_deps=deps,
                    )
                    await db.commit()
                    return execution
                except ShortRetryCredit:
                    await db.rollback()
                    continue
                except BaseException:
                    await db.rollback()
                    raise
    raise AssertionError("short-cover retries exhausted")


async def _add_second_short(uid, *, principal="10", restricted="90", basis="90",
                            gold="1000", foreign="1000"):
    """A second pair with its own short lock, to prove cover cannot touch it."""
    async with async_session_maker() as db:
        pair = FxPair(
            currency_code=uuid4().hex[:16], currency_name="T2",
            status="trading", gold_reserve=D(gold), foreign_reserve=D(foreign),
            buy_fee_rate=ZERO, sell_fee_rate=ZERO,
            short_lending_limit_foreign=D("100000"),
        )
        db.add(pair)
        await db.flush()
        db.add(FxShortPosition(
            user_id=uid, pair_id=pair.id,
            principal_foreign=D(principal), interest_foreign=ZERO,
            interest_last_accrued_at=datetime.now(timezone.utc),
            restricted_gold=D(restricted), proceeds_basis_gold=D(basis),
        ))
        await db.commit()
        return int(pair.id)


async def _stocks(db, pid):
    pair = await db.get(FxPair, pid)
    treasury = (await db.execute(select(FxTreasury).where(
        FxTreasury.pair_id == pid))).scalars().one()
    cash = (await db.execute(select(func.coalesce(func.sum(User.cash), ZERO)))).scalar_one()
    wallet = (await db.execute(select(func.coalesce(
        func.sum(FxWallet.foreign_amount), ZERO)))).scalar_one()
    locked = (await db.execute(select(func.coalesce(
        func.sum(FxShortPosition.restricted_gold), ZERO)))).scalar_one()
    principal = (await db.execute(select(func.coalesce(
        func.sum(FxShortPosition.principal_foreign), ZERO)).where(
            FxShortPosition.pair_id == pid))).scalar_one()
    pool_gold, pool_foreign = D(pair.gold_reserve), D(pair.foreign_reserve)
    return {
        "pool_gold": pool_gold, "pool_foreign": pool_foreign,
        "treasury_gold": D(treasury.gold_balance),
        "treasury_foreign": D(treasury.foreign_balance),
        "cash": D(cash), "wallet_foreign": D(wallet), "locked": D(locked),
        "principal": D(principal), "price": pool_gold / pool_foreign,
        "pool_version": int(pair.pool_version),
    }


async def _full_state(db, uid, pid):
    user = await db.get(User, uid)
    pair = await db.get(FxPair, pid)
    treasury = (await db.execute(select(FxTreasury).where(
        FxTreasury.pair_id == pid))).scalars().one()
    shorts = (await db.execute(
        select(FxShortPosition).order_by(FxShortPosition.pair_id))).scalars().all()
    trades = (await db.execute(select(FxTrade).order_by(FxTrade.id))).scalars().all()
    audits = (await db.execute(select(AuditEvent).order_by(AuditEvent.id))).scalars().all()
    return {
        "cash": D(user.cash), "debt": D(user.debt),
        "version": int(user.economic_version),
        "pool_gold": D(pair.gold_reserve), "pool_foreign": D(pair.foreign_reserve),
        "pool_version": int(pair.pool_version),
        "treasury_gold": D(treasury.gold_balance),
        "treasury_foreign": D(treasury.foreign_balance),
        "shorts": [
            (int(s.pair_id), D(s.principal_foreign), D(s.interest_foreign),
             D(s.restricted_gold), D(s.proceeds_basis_gold))
            for s in shorts
        ],
        "trade_ids": [int(t.id) for t in trades],
        "trade_purposes": [t.purpose for t in trades],
        "audit_types": [a.event_type for a in audits],
    }


async def _position(db, uid, pid):
    return (await db.execute(select(FxShortPosition).where(
        FxShortPosition.user_id == uid,
        FxShortPosition.pair_id == pid,
    ))).scalars().one()


# ── scenario 1: open + add conserve real stock, move price, no wallet ─────────

async def test_open_and_add_conserve_stock_move_price_and_keep_wallet_empty():
    uid, pid = await _seed(cash="1000", treasury_foreign="100000", limit="100000")
    async with async_session_maker() as db:
        before = await _stocks(db, pid)
        assert before["principal"] == ZERO and before["wallet_foreign"] == ZERO

    first = await _open(uid, pid, "100")
    assert first.replay is False
    async with async_session_maker() as db:
        after_first = await _stocks(db, pid)
        pos = await _position(db, uid, pid)
        trade = (await db.execute(select(FxTrade).where(
            FxTrade.id == first.trade_id))).scalars().one()
        assert trade.purpose == "short_open" and trade.side == "sell"
        assert trade.source == "player"
        assert D(trade.requested_foreign_amount) == D("100")
        assert D(trade.input_amount) == D("100")
        assert pos.principal_foreign == D("100")
        assert pos.restricted_gold == D(trade.output_amount)
        assert pos.proceeds_basis_gold == D(trade.output_amount)
        assert (await db.execute(select(FxWallet))).scalars().all() == []
        first_output = D(trade.output_amount)

    # Real gold/foreign stock (pool + treasury + users/wallets) is unchanged.
    assert (after_first["pool_gold"] + after_first["treasury_gold"] + after_first["cash"]
            == before["pool_gold"] + before["treasury_gold"] + before["cash"])
    assert (after_first["pool_foreign"] + after_first["treasury_foreign"]
            + after_first["wallet_foreign"]
            == before["pool_foreign"] + before["treasury_foreign"]
            + before["wallet_foreign"])
    assert after_first["price"] < before["price"]
    assert after_first["locked"] <= after_first["cash"]
    assert after_first["cash"] == before["cash"] + first_output
    assert after_first["treasury_foreign"] == before["treasury_foreign"] - D("100")
    assert after_first["pool_foreign"] == before["pool_foreign"] + D("100")
    assert after_first["pool_version"] == before["pool_version"] + 1

    second = await _open(uid, pid, "50")
    async with async_session_maker() as db:
        after_second = await _stocks(db, pid)
        pos = await _position(db, uid, pid)
        second_trade = (await db.execute(select(FxTrade).where(
            FxTrade.id == second.trade_id))).scalars().one()
        second_output = D(second_trade.output_amount)
        assert pos.principal_foreign == D("150")
        assert pos.restricted_gold == first_output + second_output
        assert pos.proceeds_basis_gold == first_output + second_output

    assert (after_second["pool_gold"] + after_second["treasury_gold"] + after_second["cash"]
            == before["pool_gold"] + before["treasury_gold"] + before["cash"])
    assert (after_second["pool_foreign"] + after_second["treasury_foreign"]
            + after_second["wallet_foreign"]
            == before["pool_foreign"] + before["treasury_foreign"]
            + before["wallet_foreign"])
    assert after_second["price"] < after_first["price"]
    assert after_second["locked"] <= after_second["cash"]
    assert after_second["cash"] == before["cash"] + after_second["locked"]


# ── scenario 1b: fee leg, conservation and treasury after-state audit ────────

async def test_sell_fee_open_conserves_foreign_and_audits_treasury_after():
    """A fee-bearing open must return the foreign fee to treasury, keep
    pool+treasury+wallet foreign conserved, credit exactly the net gold P once,
    and persist a treasury_after snapshot the generic FX replay can anchor.

    A regression here means the fee leg is dropped (money created/destroyed),
    proceeds are double-credited, or the audit after-state is missing so replay
    folds the treasury to zero and reports false mismatches on later pair
    events.
    """
    uid, pid = await _seed(cash="1000", sell_fee="0.02", treasury_foreign="100000",
                           gold="1000", foreign="1000", limit="100000")
    async with async_session_maker() as db:
        before = await _stocks(db, pid)
        user_before = await db.get(User, uid)

    key = "k-fee"
    opened = await _open(uid, pid, "100", key=key)

    async with async_session_maker() as db:
        after = await _stocks(db, pid)
        user = await db.get(User, uid)
        pos = await _position(db, uid, pid)
        trade = (await db.execute(select(FxTrade).where(
            FxTrade.id == opened.trade_id))).scalars().one()
        treasury = (await db.execute(select(FxTreasury).where(
            FxTreasury.pair_id == pid))).scalars().one()
        audit = (await db.execute(select(AuditEvent).where(
            AuditEvent.event_type == "fx_trade",
            AuditEvent.ref_id == trade.id))).scalars().one()
        events = (await db.execute(select(AuditEvent).order_by(AuditEvent.id))).scalars().all()

        borrowed = D(trade.input_amount)
        fee = D(trade.fee_amount)
        proceeds = D(trade.output_amount)
        assert borrowed == D("100")
        assert fee > 0

        # Foreign: treasury pays Q out and the fee comes back; pool receives Q-fee.
        assert D(treasury.foreign_balance) == before["treasury_foreign"] - borrowed + fee
        assert D(treasury.foreign_balance) == after["treasury_foreign"]
        assert D(trade.post_foreign_reserve) == before["pool_foreign"] + borrowed - fee
        assert D(trade.post_foreign_reserve) == after["pool_foreign"]
        assert (after["pool_foreign"] + after["treasury_foreign"] + after["wallet_foreign"]
                == before["pool_foreign"] + before["treasury_foreign"]
                + before["wallet_foreign"])

        # Gold: net P enters cash exactly once and locks the target once; stock conserved.
        assert D(user.cash) == D(user_before.cash) + proceeds
        assert D(pos.principal_foreign) == borrowed
        assert D(pos.restricted_gold) == proceeds
        assert D(pos.proceeds_basis_gold) == proceeds
        assert (after["pool_gold"] + after["treasury_gold"] + D(user.cash)
                == before["pool_gold"] + before["treasury_gold"] + D(user_before.cash))

        # Replay must anchor the short event's treasury from the audit after-state,
        # not (0, 0); otherwise this and every later pair event mismatch live.
        snap, mismatches = audit_replay.fold(events, check=True)
        assert mismatches == []
        assert await audit_replay.compare_with_live(db, snap) == []

        # Spec §12 identity + after-state pinned to the committed rows.
        payload = audit.payload
        assert payload["purpose"] == "short_open"
        assert D(payload["fee_amount"]) == fee
        assert payload["idempotency_key"] == key
        assert payload["user_economic_version"] == int(user.economic_version)
        assert payload["pool_version"] == int(after["pool_version"])
        assert payload["wallet_after"] is None
        after_state = payload["treasury_after"]
        assert D(after_state["gold"]) == D(treasury.gold_balance)
        assert D(after_state["foreign"]) == D(treasury.foreign_balance)
        # The generic fx_trade fold anchors on the record_fx_trade key spelling.
        assert D(after_state["gold_balance"]) == D(treasury.gold_balance)
        assert D(after_state["foreign_balance"]) == D(treasury.foreign_balance)


# ── scenario 2: settle old foreign debt at T before adding new principal ─────

async def test_old_foreign_debt_accrues_at_T_without_charging_new_principal():
    uid, pid = await _seed(
        cash="1000", rate="0.1", treasury_foreign="100000000",
        limit="100000000",
        short={"principal": "100", "accrued_ago_sec": 86400},
    )
    await _open(uid, pid, "10000")

    async with async_session_maker() as db:
        pos = await _position(db, uid, pid)
        # ~10% of the old 100 only: if the 10,000 new principal inherited the
        # day-old clock the interest would be ~1,010.
        interest = D(pos.interest_foreign)
        assert D("9") < interest < D("100")
        assert pos.principal_foreign == D("10100")
        clock = _utc(pos.interest_last_accrued_at)
        assert clock is not None
        assert (datetime.now(timezone.utc) - clock).total_seconds() < 120


async def test_dust_timebase_still_resets_before_new_principal():
    old_clock = datetime.now(timezone.utc) - timedelta(seconds=86400)
    uid, pid = await _seed(
        cash="1000", rate="0.1", treasury_foreign="100000000",
        limit="100000000",
        short={"principal": "0.000001", "accrued_ago_sec": 86400},
    )
    async with async_session_maker() as db:
        before_interest = D((await _position(db, uid, pid)).interest_foreign)
        assert before_interest == ZERO  # old day rounds below one unit

    await _open(uid, pid, "10000")

    async with async_session_maker() as db:
        pos = await _position(db, uid, pid)
        assert D(pos.interest_foreign) == ZERO
        clock = _utc(pos.interest_last_accrued_at)
        assert clock is not None
        # Even though no interest was persisted, the new principal must start
        # at T rather than at the old dusty clock.
        assert clock > old_clock + timedelta(hours=23)
        assert (datetime.now(timezone.utc) - clock).total_seconds() < 120
        assert pos.principal_foreign == D("10000.000001")


async def test_gold_debt_settles_at_T_and_proceeds_do_not_repay_it():
    uid, pid = await _seed(cash="1000", debt="100", debt_ago_sec=86400,
                           rate="0.1", treasury_foreign="100000",
                           limit="100000")
    opened = await _open(uid, pid, "100")

    async with async_session_maker() as db:
        user = await db.get(User, uid)
        pos = await _position(db, uid, pid)
        # ~10% of the day-old 100 is accrued...
        assert D("109") < D(user.debt) < D("111")
        assert _utc(user.debt_last_accrued_at) > datetime.now(timezone.utc) - timedelta(minutes=2)
        # ...and the opening proceeds are not used to repay it.
        trade = (await db.execute(select(FxTrade).where(
            FxTrade.id == opened.trade_id))).scalars().one()
        assert D(user.cash) == D("1000") + D(trade.output_amount)
        assert pos.principal_foreign == D("100")
        interest_events = (await db.execute(select(AuditEvent).where(
            AuditEvent.event_type == "interest_accrual"))).scalars().all()
        assert any(e.payload.get("source") == "fx_short_open"
                   for e in interest_events), "gold settlement must be audited"


# ── scenario 3: rejections leave every persisted quantity untouched ──────────

@pytest.mark.parametrize("overrides, detail", [
    ({"treasury_foreign": "50"}, "treasury"),
    ({"limit": "50"}, "limit"),
])
async def test_insufficient_treasury_or_limit_changes_nothing(overrides, detail):
    uid, pid = await _seed(cash="1000", **overrides)
    async with async_session_maker() as db:
        before = await _full_state(db, uid, pid)

    with pytest.raises(HTTPException) as exc:
        await _open(uid, pid, "100")
    assert exc.value.status_code == 400
    assert detail in str(exc.value.detail)

    async with async_session_maker() as db:
        after = await _full_state(db, uid, pid)
    assert after == before


async def test_failed_post_trade_shared_margin_changes_nothing():
    uid, pid = await _seed(cash="0", sell_fee="0.02", treasury_foreign="100000",
                           limit="100000")
    async with async_session_maker() as db:
        before = await _full_state(db, uid, pid)

    with pytest.raises(HTTPException) as exc:
        await _open(uid, pid, "500")
    assert exc.value.status_code == 400
    assert "insufficient_initial_margin" in str(exc.value.detail)

    async with async_session_maker() as db:
        after = await _full_state(db, uid, pid)
    assert after == before


# ── scenario 4: idempotent replay and cross-purpose/parameter conflict ───────

async def test_same_key_replays_one_borrow_trade_and_audit():
    uid, pid = await _seed(cash="1000", treasury_foreign="100000", limit="100000")
    first = await _open(uid, pid, "100", key="k-replay")
    second = await _open(uid, pid, "100", key="k-replay")

    assert second.replay is True
    assert second.trade_id == first.trade_id
    assert second.post_price == first.post_price

    async with async_session_maker() as db:
        trades = (await db.execute(select(FxTrade).where(
            FxTrade.user_id == uid,
            FxTrade.idempotency_key == "k-replay",
        ))).scalars().all()
        assert len(trades) == 1
        audits = (await db.execute(select(AuditEvent).where(
            AuditEvent.event_type == "fx_trade"))).scalars().all()
        assert len(audits) == 1
        assert audits[0].payload["purpose"] == "short_open"
        pos = await _position(db, uid, pid)
        assert pos.principal_foreign == D("100")
        treasury = (await db.execute(select(FxTreasury).where(
            FxTreasury.pair_id == pid))).scalars().one()
        assert D(treasury.foreign_balance) == D("100000") - D("100")


async def test_spot_and_short_same_key_conflicts_without_second_borrow():
    uid, pid = await _seed(cash="100000", treasury_foreign="100000", limit="100000")
    async with async_session_maker() as db:
        await trading.execute_trade(db, uid, pid, "buy", D("10"), D("0"), "k-cross")

    with pytest.raises(HTTPException) as exc:
        await _open(uid, pid, "100", key="k-cross")
    assert exc.value.status_code == 409

    async with async_session_maker() as db:
        trades = (await db.execute(select(FxTrade).where(
            FxTrade.user_id == uid))).scalars().all()
        assert len(trades) == 1 and trades[0].purpose == "spot"
        assert (await db.execute(select(FxShortPosition))).scalars().all() == []


async def test_same_key_with_changed_amount_or_min_out_conflicts():
    uid, pid = await _seed(cash="1000", treasury_foreign="100000", limit="100000")
    await _open(uid, pid, "100", key="k-param")

    with pytest.raises(HTTPException) as amount_exc:
        await _open(uid, pid, "101", key="k-param")
    assert amount_exc.value.status_code == 409

    with pytest.raises(HTTPException) as min_exc:
        await _open(uid, pid, "100", min_out="1", key="k-param")
    assert min_exc.value.status_code == 409

    async with async_session_maker() as db:
        assert len((await db.execute(select(FxTrade).where(
            FxTrade.user_id == uid))).scalars().all()) == 1
        pos = await _position(db, uid, pid)
        assert pos.principal_foreign == D("100")


# ── additional persisted guards from the precondition list ───────────────────

async def test_opening_gate_missing_is_disabled_by_default_and_changes_nothing():
    uid, pid = await _seed(cash="1000", treasury_foreign="100000", limit="100000",
                           short_enabled=False)
    async with async_session_maker() as db:
        before = await _full_state(db, uid, pid)

    with pytest.raises(HTTPException) as exc:
        await _open(uid, pid, "100")
    assert exc.value.status_code == 403
    assert "disabled" in str(exc.value.detail).lower()

    async with async_session_maker() as db:
        after = await _full_state(db, uid, pid)
    assert after == before


async def test_positive_spot_wallet_blocks_short_without_mutation():
    uid, pid = await _seed(cash="1000", treasury_foreign="100000", limit="100000",
                           wallet_foreign="5")
    async with async_session_maker() as db:
        before = await _full_state(db, uid, pid)

    with pytest.raises(HTTPException) as exc:
        await _open(uid, pid, "100")
    assert exc.value.status_code == 400

    async with async_session_maker() as db:
        after = await _full_state(db, uid, pid)
    assert after == before
    assert not GATES.held_keys()


# ══════════════════════════════════════════════════════════════════════════════
# Task 3b: exact-output short cover ledger (user-requested quantity / full)
# ══════════════════════════════════════════════════════════════════════════════

async def test_partial_then_full_cover_conserves_stock_and_settles_interest():
    """Buy exactly q foreign back into treasury; only gold/foreign stock moves.

    A regression here means the AMM leg, the treasury return, the fee currency,
    the lock release or the debt repayment is booked against the wrong side, or
    accrued interest is dropped when cover_all locks the tail.
    """
    uid, pid = await _seed(
        cash="1000", rate="0.1", buy_fee="0.02",
        treasury_foreign="100000", gold="1000", foreign="1000", limit="100000",
        short={"principal": "100", "interest": "0", "restricted": "100",
               "basis": "100", "accrued_ago_sec": 86400},
    )
    async with async_session_maker() as db:
        before = await _stocks(db, pid)
        pre_pos = await _position(db, uid, pid)
        pre_lock = D(pre_pos.restricted_gold)
        pre_basis = D(pre_pos.proceeds_basis_gold)

    partial = await _cover(uid, pid, q="50", key="k-cover-1")

    async with async_session_maker() as db:
        after = await _stocks(db, pid)
        pos = await _position(db, uid, pid)
        trade = (await db.execute(select(FxTrade).where(
            FxTrade.id == partial.trade_id))).scalars().one()
        audit = (await db.execute(select(AuditEvent).where(
            AuditEvent.event_type == "fx_trade",
            AuditEvent.ref_id == trade.id))).scalars().one()
        payload = audit.payload
        q = D(trade.output_amount)
        x = D(trade.input_amount)
        fee = D(trade.fee_amount)

        assert trade.purpose == "short_cover" and trade.side == "buy"
        assert trade.source == "player"
        assert D(trade.requested_foreign_amount) == D("50")
        assert trade.cover_all is None
        assert q == D("50") and fee > 0 and x > 0

        # Exact physical legs: AMM takes X gold (net + fee), treasury gains the
        # gold fee and the exact q foreign returned, wallet never moves.
        assert D(after["treasury_foreign"]) == before["treasury_foreign"] + q
        assert D(after["pool_foreign"]) == before["pool_foreign"] - q
        assert D(after["pool_gold"]) == before["pool_gold"] + x - fee
        assert D(after["treasury_gold"]) == before["treasury_gold"] + fee
        assert D(after["cash"]) == before["cash"] - x
        assert D(after["wallet_foreign"]) == ZERO
        assert D(trade.post_foreign_reserve) == before["pool_foreign"] - q
        assert (after["pool_gold"] + after["treasury_gold"] + after["cash"]
                == before["pool_gold"] + before["treasury_gold"] + before["cash"])
        assert (after["pool_foreign"] + after["treasury_foreign"]
                + after["wallet_foreign"]
                == before["pool_foreign"] + before["treasury_foreign"]
                + before["wallet_foreign"])

        # q repays accrued interest first (~10 for the day-old 100), then principal.
        interest_paid = D(payload["interest_paid_foreign"])
        principal_paid = D(payload["principal_paid_foreign"])
        assert D("9.9") < interest_paid < D("10.1")
        assert interest_paid + principal_paid == q
        assert D(pos.interest_foreign) == ZERO
        assert D(payload["repaid_foreign"]) == q

        # Lock release and proceeds basis apportion independently; realized P/L is
        # allocated basis minus real gold paid, never lock release minus X.
        q_effective = q + D(pos.principal_foreign) + D(pos.interest_foreign)
        expected_release = amount_down(pre_lock * q / q_effective)
        expected_basis = amount_down(pre_basis * q / q_effective)
        assert D(payload["released_lock"]) == expected_release
        assert D(pos.restricted_gold) == pre_lock - expected_release
        assert D(payload["allocated_proceeds_basis"]) == expected_basis
        assert D(pos.proceeds_basis_gold) == pre_basis - expected_basis
        assert D(payload["realized_pl"]) == expected_basis - x
        assert D(after["pool_version"]) == before["pool_version"] + 1

    # Full cover locks the entire post-settlement debt and clears every tail.
    full = await _cover(uid, pid, cover_all=True, max_gold_in="100000",
                        key="k-cover-2")
    async with async_session_maker() as db:
        after_full = await _stocks(db, pid)
        pos_full = await _position(db, uid, pid)
        trade_full = (await db.execute(select(FxTrade).where(
            FxTrade.id == full.trade_id))).scalars().one()
        q_full = D(trade_full.output_amount)
        assert trade_full.cover_all is True
        assert trade_full.requested_foreign_amount is None
        assert q_full > ZERO
        assert D(pos_full.principal_foreign) == ZERO
        assert D(pos_full.interest_foreign) == ZERO
        assert D(pos_full.restricted_gold) == ZERO
        assert D(pos_full.proceeds_basis_gold) == ZERO
        assert pos_full.interest_last_accrued_at is None
        assert D(after_full["treasury_foreign"]) == D(after["treasury_foreign"]) + q_full
        assert D(after_full["pool_foreign"]) == D(after["pool_foreign"]) - q_full
        assert D(after_full["locked"]) == ZERO
        assert (after_full["pool_gold"] + after_full["treasury_gold"] + after_full["cash"]
                == before["pool_gold"] + before["treasury_gold"] + before["cash"])
        assert (after_full["pool_foreign"] + after_full["treasury_foreign"]
                + after_full["wallet_foreign"]
                == before["pool_foreign"] + before["treasury_foreign"]
                + before["wallet_foreign"])


async def test_losing_partial_cover_consumes_own_lock_only_and_uses_basis():
    """A losing cover may spend extra gold from *this* lock, never another short.

    The realized loss uses the historical proceeds basis, not the extra lock
    released, so a loss cannot be hidden or turned into a book profit.
    """
    uid, pid = await _seed(
        cash="120", rate="0", buy_fee="0",
        treasury_foreign="100000", gold="1000", foreign="205", limit="100000",
        short={"principal": "10", "interest": "0", "restricted": "20",
               "basis": "5", "accrued_ago_sec": 0},
    )
    other_pid = await _add_second_short(uid, restricted="90", basis="90")

    execution = await _cover(uid, pid, q="5", key="k-loss")

    async with async_session_maker() as db:
        target = await _position(db, uid, pid)
        other = await _position(db, uid, other_pid)
        trade = (await db.execute(select(FxTrade).where(
            FxTrade.id == execution.trade_id))).scalars().one()
        audit = (await db.execute(select(AuditEvent).where(
            AuditEvent.event_type == "fx_trade",
            AuditEvent.ref_id == trade.id))).scalars().one()
        payload = audit.payload
        x = D(trade.input_amount)
        # G=1000, F=205, fee=0, q=5 -> X=25; F_cash=10, S_base=10, so 15 of the
        # target's own lock is released and the other pair's 90 is untouched.
        assert x == D("25")
        assert D(target.restricted_gold) == D("5")
        assert D(target.proceeds_basis_gold) == D("2.5")
        assert D(other.restricted_gold) == D("90")
        assert D(other.proceeds_basis_gold) == D("90")
        assert D((await db.get(User, uid)).cash) == D("95")
        assert D(payload["released_lock"]) == D("15")
        assert D(payload["allocated_proceeds_basis"]) == D("2.5")
        assert D(payload["realized_pl"]) == D("-22.5")
        # Fault mode: substituting the released lock for historical basis gives
        # -10; the ledger must book the basis-based -22.5 instead.
        assert D(payload["realized_pl"]) != D(payload["released_lock"]) - x


async def test_cover_all_settles_tail_replays_and_rejects_conflicts():
    """cover_all locks the current interest, replays as a no-op, and conflicts.

    A regression means a fully closed position loses the interest tail (dust),
    a retry buys foreign again, or a changed request/other purpose is treated as
    the same idempotent operation.
    """
    uid, pid = await _seed(
        cash="1000", rate="0.1", buy_fee="0.02",
        treasury_foreign="100000", gold="1000", foreign="1000", limit="100000",
        short={"principal": "100", "interest": "0", "restricted": "100",
               "basis": "100", "accrued_ago_sec": 86400},
    )
    first = await _cover(uid, pid, cover_all=True, max_gold_in="1000000", key="k-all")

    async with async_session_maker() as db:
        before_replay = await _full_state(db, uid, pid)
        pos = await _position(db, uid, pid)
        first_trade = (await db.execute(select(FxTrade).where(
            FxTrade.id == first.trade_id))).scalars().one()
        # cover_all took the post-settlement principal+interest (>100), so the
        # day-old interest cannot survive as a dust remainder.
        assert D(first_trade.output_amount) > D("100")
        assert D(pos.principal_foreign) == ZERO
        assert D(pos.interest_foreign) == ZERO
        assert D(pos.restricted_gold) == ZERO
        assert D(pos.proceeds_basis_gold) == ZERO
        assert pos.interest_last_accrued_at is None

    replay = await _cover(uid, pid, cover_all=True, max_gold_in="1000000", key="k-all")
    assert replay.replay is True
    assert replay.trade_id == first.trade_id
    async with async_session_maker() as db:
        after_replay = await _full_state(db, uid, pid)
    assert after_replay == before_replay  # no second buy / audit on replay

    # Same key, different request identity: cover_all vs fixed q and a changed
    # max_gold_in must both conflict instead of mutating a later position.
    with pytest.raises(HTTPException) as fixed_exc:
        await _cover(uid, pid, q="50", key="k-all")
    assert fixed_exc.value.status_code == 409
    with pytest.raises(HTTPException) as max_exc:
        await _cover(uid, pid, cover_all=True, max_gold_in="999", key="k-all")
    assert max_exc.value.status_code == 409

    # Cross-purpose: a spot key cannot be reused for a cover.
    async with async_session_maker() as db:
        await trading.execute_trade(db, uid, pid, "buy", D("1"), D("0"), "k-spot-cover")
    with pytest.raises(HTTPException) as cross_exc:
        await _cover(uid, pid, q="1", key="k-spot-cover")
    assert cross_exc.value.status_code == 409


@pytest.mark.parametrize("seed_overrides, cover_kwargs, kind", [
    ({"cash": "20", "foreign": "205",
      "short": {"principal": "10", "interest": "0", "restricted": "15",
                "basis": "15"}}, {"q": "5"}, "cash"),
    ({"buy_fee": "0.02"}, {"q": "50", "max_gold_in": "0"}, "max_gold_in"),
    ({"foreign": "5"}, {"q": "5"}, "pool"),
    ({}, {"q": "200"}, "excess"),
])
async def test_cover_rejections_change_nothing(seed_overrides, cover_kwargs, kind):
    """Insufficient cash, a too-low cap, q>=F or q>debt leaves the whole book.

    A regression here means a rejected cover still moves cash, debt, locks,
    pool, treasury, trades or audit rows (a partial ledger).
    """
    seed_kwargs = dict(
        cash="1000", rate="0", treasury_foreign="100000",
        gold="1000", foreign="1000", limit="100000",
        short={"principal": "100", "interest": "0", "restricted": "100",
               "basis": "100"},
    )
    seed_kwargs.update(seed_overrides)
    uid, pid = await _seed(**seed_kwargs)
    async with async_session_maker() as db:
        before = await _full_state(db, uid, pid)

    if kind == "pool":
        with pytest.raises(ShortCoverRejected):
            await _cover(uid, pid, key="k-rej", **cover_kwargs)
    else:
        with pytest.raises(HTTPException) as exc:
            await _cover(uid, pid, key="k-rej", **cover_kwargs)
        assert exc.value.status_code == (409 if kind == "max_gold_in" else 400)

    async with async_session_maker() as db:
        after = await _full_state(db, uid, pid)
    assert after == before
    assert not GATES.held_keys()


@pytest.mark.parametrize("status", ["trading", "paused"])
async def test_reduce_only_allows_cover_but_blocks_ordinary_buy(status):
    """Cover is risk-reducing, so reduce_only permits it; a spot buy still 403s."""
    uid, pid = await _seed(
        cash="1000", rate="0", buy_fee="0",
        treasury_foreign="100000", gold="1000", foreign="1000", limit="100000",
        status=status, reduce_only=True,
        short={"principal": "10", "interest": "0", "restricted": "100",
               "basis": "100"},
    )
    execution = await _cover(uid, pid, q="5", key="k-ro-cover")
    assert execution.replay is False
    async with async_session_maker() as db:
        pos = await _position(db, uid, pid)
        assert D(pos.principal_foreign) == D("5")

    with pytest.raises(HTTPException) as exc:
        async with async_session_maker() as db:
            await trading.execute_trade(db, uid, pid, "buy", D("1"), D("0"), "k-ro-buy")
    assert exc.value.status_code == 403


@pytest.mark.parametrize("status, reduce_only, archived", [
    ("paused", False, False),
    ("closed", False, False),
    ("trading", False, True),
])
async def test_non_coverable_pair_state_blocks_cover_and_retains_debt(
        status, reduce_only, archived):
    """paused without reduce_only, closed and archived keep the obligation but
    block user cover; no leg of the book may move."""
    uid, pid = await _seed(
        cash="1000", rate="0",
        treasury_foreign="100000", gold="1000", foreign="1000", limit="100000",
        status=status, reduce_only=reduce_only, archived=archived,
        short={"principal": "10", "interest": "0", "restricted": "100",
               "basis": "100"},
    )
    async with async_session_maker() as db:
        before = await _full_state(db, uid, pid)

    with pytest.raises(HTTPException) as exc:
        await _cover(uid, pid, q="5", key="k-blocked")
    assert exc.value.status_code == 403

    async with async_session_maker() as db:
        after = await _full_state(db, uid, pid)
    assert after == before
    assert after["shorts"][0][1] == D("10")  # debt retained


async def test_cover_allowed_with_gates_off_frozen_and_below_margin():
    """The opening gate, loan gate and credit freeze never block a cover.

    Cover is the only way an underwater, frozen short can be reduced; closing
    those gates must not trap the obligation.
    """
    uid, pid = await _seed(
        cash="1000", debt="2000", debt_ago_sec=86400, rate="0.1",
        short_enabled=False, loan_enabled=False, credit_frozen=True,
        treasury_foreign="100000", gold="1000", foreign="1000", limit="100000",
        short={"principal": "100", "interest": "0", "restricted": "100",
               "basis": "100", "accrued_ago_sec": 86400},
    )
    execution = await _cover(uid, pid, q="50", max_gold_in="1000", key="k-off")
    assert execution.replay is False
    async with async_session_maker() as db:
        user = await db.get(User, uid)
        pos = await _position(db, uid, pid)
        assert D(pos.principal_foreign) < D("100")
        assert D(pos.interest_foreign) == ZERO
        assert D(user.cash) < D("1000")
        # Gold interest still settles at T even with the loan gate closed, but
        # the cover never borrows the ~53 gold it spends.
        assert D(user.debt) > D("2000")
        assert D(user.debt) < D("2210")


async def test_cover_without_short_rejects_and_creates_nothing():
    """No outstanding debt means no cover and never an implicit spot buy."""
    uid, pid = await _seed(cash="1000", rate="0", treasury_foreign="100000",
                           gold="1000", foreign="1000", limit="100000")
    async with async_session_maker() as db:
        before = await _full_state(db, uid, pid)

    with pytest.raises(HTTPException) as exc:
        await _cover(uid, pid, q="5", key="k-noshort")
    assert exc.value.status_code == 400
    assert "short" in str(exc.value.detail).lower()

    async with async_session_maker() as db:
        after = await _full_state(db, uid, pid)
        assert after == before
        assert (await db.execute(select(FxWallet))).scalars().all() == []
        assert (await db.execute(select(FxShortPosition))).scalars().all() == []
    assert not GATES.held_keys()


async def test_cover_request_shape_must_be_fixed_or_cover_all():
    """cover_all and a fixed quantity are mutually exclusive and one is required."""
    uid, pid = await _seed(
        cash="1000", rate="0", treasury_foreign="100000",
        gold="1000", foreign="1000", limit="100000",
        short={"principal": "10", "interest": "0", "restricted": "100",
               "basis": "100"},
    )
    async with async_session_maker() as db:
        before = await _full_state(db, uid, pid)

    with pytest.raises(ShortCoverRejected):
        await _cover(uid, pid, q="5", cover_all=True, key="k-shape")
    with pytest.raises(ShortCoverRejected):
        await _cover(uid, pid, q=None, cover_all=False, key="k-shape")

    async with async_session_maker() as db:
        after = await _full_state(db, uid, pid)
    assert after == before
    assert not GATES.held_keys()


# ── fix round 1: cover rejection type and accumulator storage bounds ─────────

@pytest.mark.parametrize("cover_kwargs", [
    {"q": "NaN"},
    {"q": "0"},
    {"q": "-1"},
    {"q": "1.0000001"},
    {"q": "5", "max_gold_in": "NaN"},
    {"q": "5", "max_gold_in": "-1"},
    {"q": "5", "max_gold_in": "1.0000001"},
    {"q": "5", "max_gold_in": "10000000000"},
])
async def test_malformed_cover_numbers_are_cover_rejections(cover_kwargs):
    """Every bad cover number is a documented cover rejection, not an open one.

    A future cover route catches ``ShortCoverRejected`` (or the shared
    ``ShortRejected`` base) to answer 4xx.  NaN, zero, negative or over-precise
    quantities must not leak the open-path type and become a 500, and a rejected
    shape must leave the persisted book untouched.
    """
    uid, pid = await _seed(
        cash="1000", rate="0", treasury_foreign="100000",
        gold="1000", foreign="1000", limit="100000",
        short={"principal": "100", "interest": "0", "restricted": "100",
               "basis": "100"},
    )
    async with async_session_maker() as db:
        before = await _full_state(db, uid, pid)

    with pytest.raises(ShortCoverRejected):
        await _cover(uid, pid, key="k-bad", **cover_kwargs)

    async with async_session_maker() as db:
        after = await _full_state(db, uid, pid)
    assert after == before
    assert not GATES.held_keys()


async def test_cover_treasury_gold_overflow_rejects_before_any_mutation():
    """A gold fee that overflows the treasury Numeric(16,6) rejects atomically.

    ``quote_buy_exact_out`` bounds X and the pool, but not treasury.gold_balance.
    SQLite silently stores a seven-integer-digit Numeric while Postgres would
    fail at flush; the cover must precheck the accumulator and reject as a cover
    error with no money/debt/lock/pool/treasury/trade/audit change.
    """
    near_max = D("9999999999.9")
    uid, pid = await _seed(
        cash="1000", rate="0", buy_fee="0.02",
        treasury_foreign="100000", gold="1000", foreign="1000",
        treasury_gold=near_max, limit="100000",
        short={"principal": "10", "interest": "0", "restricted": "10",
               "basis": "10"},
    )
    async with async_session_maker() as db:
        before = await _full_state(db, uid, pid)

    with pytest.raises(ShortCoverRejected):
        await _cover(uid, pid, q="5", key="k-treasury-overflow")

    async with async_session_maker() as db:
        after = await _full_state(db, uid, pid)
    assert after == before
    assert not GATES.held_keys()


@pytest.mark.parametrize("operation", ["open", "cover"])
async def test_short_trade_audit_preserves_one_authoritative_accrual_clock(operation):
    """Historical replay must accrue both debt currencies at the persisted T."""
    uid, pid = await _seed(
        cash="1000", debt="100", debt_ago_sec=86400, rate="0.1",
        short={"principal": "100", "restricted": "100", "basis": "100",
               "accrued_ago_sec": 86400},
    )
    async with async_session_maker() as db:
        before_clock = _utc((await db.get(User, uid)).debt_last_accrued_at)
    if operation == "open":
        await _open(uid, pid, "10")
    else:
        await _cover(uid, pid, q="5")
    async with async_session_maker() as db:
        user = await db.get(User, uid)
        pos = await _position(db, uid, pid)
        events = (await db.execute(select(AuditEvent).where(
            AuditEvent.user_id == uid).order_by(AuditEvent.id))).scalars().all()
        trade = next(e for e in events if e.event_type == "fx_trade")
        gold = next(e for e in events if e.event_type == "interest_accrual"
                    and e.payload.get("currency") != "foreign")
        foreign = next(e for e in events if e.payload.get("currency") == "foreign")
        authoritative = _utc(pos.interest_last_accrued_at)
        assert D(user.debt) > D("100")
        assert D(foreign.payload["interest_foreign_delta"]) > 0
        assert _utc(user.debt_last_accrued_at) == authoritative
        assert _utc(trade.ts) == datetime.fromisoformat(trade.payload["accrued_at"]) == authoritative
        assert _utc(gold.ts) == datetime.fromisoformat(gold.payload["accrued_at"]) == authoritative
        assert datetime.fromisoformat(gold.payload["debt_last_accrued_at_before"]) == before_clock
        assert datetime.fromisoformat(gold.payload["debt_last_accrued_at_after"]) == authoritative
        assert _utc(foreign.ts) == authoritative
        assert gold.id < foreign.id < trade.id
