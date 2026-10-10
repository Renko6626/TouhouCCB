"""WP2b1: read-only short valuation and shared risk basis.

The tests assert actual ``AccountValuation`` numbers, group roles/executability
and read-only persistence state for fabricated ``FxShortPosition`` rows.  They
never assert source text or internal structure: a regression here means a wrong
money amount, a merged foreign unit, an unknown liability silently zeroed, or a
write during read-only valuation.
"""
import os
import sys
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, Decimal

import pytest
from sqlalchemy import event, select

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.database import async_session_maker, engine
from app.models.base import User
from app.models.fx import FxPair, FxShortPosition, FxWallet
from app.services.credit import flags as credit_flags
from app.services.credit import valuation as valuation_mod
from app.services.credit.flags import CreditFlags
from app.services.credit.valuation import value_user_detailed, value_users_batch
from app.services.fx.amm import marginal_price, quote_buy_exact_out, quote_sell
from app.services.fx.shorts import pending_short_debt

pytestmark = pytest.mark.asyncio

ZERO = Decimal("0")
Q6 = Decimal("0.000001")
RATE = Decimal("0.01")


@pytest.fixture(autouse=True)
def clean_flags():
    credit_flags.clear_flags()
    yield
    credit_flags.clear_flags()
    credit_flags.set_new_risk_frozen(None)


def _enable_unified(leverage="10", maintenance="0.05"):
    credit_flags.set_flags(CreditFlags(
        credit_leverage=Decimal(leverage),
        credit_maintenance_ratio=Decimal(maintenance),
    ))


async def _user(session, *, cash="0", debt="0", version=0):
    user = User(username=uuid.uuid4().hex, cash=Decimal(cash), debt=Decimal(debt),
                economic_version=version)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _pair(session, *, gold="100", foreign="100", status="trading",
                reduce_only=False, buy_fee="0", sell_fee="0"):
    pair = FxPair(currency_code=uuid.uuid4().hex[:16], currency_name="t",
                  status=status, reduce_only=reduce_only,
                  gold_reserve=Decimal(gold), foreign_reserve=Decimal(foreign),
                  buy_fee_rate=Decimal(buy_fee), sell_fee_rate=Decimal(sell_fee))
    session.add(pair)
    await session.commit()
    await session.refresh(pair)
    return pair


async def _short(session, user, pair, *, principal="0", interest="0",
                 restricted="0", accrued=None):
    total = Decimal(principal) + Decimal(interest)
    if accrued is None and total > 0:
        accrued = datetime.now(timezone.utc)
    row = FxShortPosition(
        user_id=user.id, pair_id=pair.id,
        principal_foreign=Decimal(principal), interest_foreign=Decimal(interest),
        restricted_gold=Decimal(restricted), proceeds_basis_gold=Decimal(restricted),
        interest_last_accrued_at=accrued,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


def _cover_groups(valuation):
    return [g for g in valuation.groups if g.role == "short_cover"]


async def test_cash_includes_restricted_proceeds_once():
    """C already contains S: equity subtracts the liability K, never adds S again."""
    async with async_session_maker() as session:
        user = await _user(session, cash="1000")
        pair = await _pair(session, gold="100", foreign="100")
        await _short(session, user, pair, principal="10", restricted="400")

        v = await value_user_detailed(session, user.id, daily_rate=RATE)
        expected_k = quote_buy_exact_out(
            Decimal("10"), Decimal("100"), Decimal("100"), ZERO,
        ).input_amount

        assert v.cash == Decimal("1000")
        assert v.restricted_cash == Decimal("400")
        assert v.available_cash == Decimal("600")
        assert v.short_cover_cost == expected_k
        assert v.liquidation_equity == (Decimal("1000") - expected_k).quantize(Q6)
        # Double counting S would produce this instead.
        assert v.liquidation_equity != (Decimal("1400") - expected_k).quantize(Q6)

        cover = _cover_groups(v)
        assert len(cover) == 1
        assert cover[0].key.product == "fx"
        assert cover[0].key.group_id == pair.id
        assert cover[0].value == expected_k
        assert cover[0].role == "short_cover"


async def test_k_uses_buy_fee_and_pending_interest_without_writing():
    async with async_session_maker() as session:
        user = await _user(session, cash="100000")
        pair = await _pair(session, gold="100", foreign="100", buy_fee="0.05")
        accrued = datetime.now(timezone.utc) - timedelta(days=30)
        row = await _short(session, user, pair, principal="10", accrued=accrued)
        row_id = row.id
        before = (row.principal_foreign, row.interest_foreign,
                  row.interest_last_accrued_at, row.restricted_gold)

        v = await value_user_detailed(session, user.id, daily_rate=RATE)

        q = pending_short_debt(row, RATE, datetime.now(timezone.utc))
        assert q > Decimal("10")  # 30 days of pending interest is included
        expected = quote_buy_exact_out(
            q, Decimal("100"), Decimal("100"), Decimal("0.05"),
        ).input_amount
        assert v.short_cover_cost == expected

        no_fee = quote_buy_exact_out(
            q, Decimal("100"), Decimal("100"), ZERO,
        ).input_amount
        principal_only = quote_buy_exact_out(
            Decimal("10"), Decimal("100"), Decimal("100"), Decimal("0.05"),
        ).input_amount
        assert v.short_cover_cost > no_fee          # buy fee raises the cost
        assert v.short_cover_cost > principal_only  # pending interest raises it

        session.expire_all()
        stored = (await session.execute(
            select(FxShortPosition).where(FxShortPosition.id == row_id)
        )).scalars().one()
        assert (stored.principal_foreign, stored.interest_foreign,
                stored.interest_last_accrued_at, stored.restricted_gold) == before


async def test_two_pairs_cover_costs_sum_without_merging_units():
    async with async_session_maker() as session:
        user = await _user(session, cash="0")
        a = await _pair(session, gold="100", foreign="100")
        b = await _pair(session, gold="50", foreign="200")
        await _short(session, user, a, principal="5")
        await _short(session, user, b, principal="7")

        v = await value_user_detailed(session, user.id, daily_rate=RATE)
        ka = quote_buy_exact_out(Decimal("5"), Decimal("100"), Decimal("100"), ZERO).input_amount
        kb = quote_buy_exact_out(Decimal("7"), Decimal("50"), Decimal("200"), ZERO).input_amount
        assert v.short_cover_cost == ka + kb
        assert v.liquidation_equity == (ZERO - (ka + kb)).quantize(Q6)

        # Merging the two currencies into one reserve curve would give a
        # different (wrong) number; assert we did not do that.
        merged = quote_buy_exact_out(
            Decimal("12"), Decimal("150"), Decimal("300"), ZERO,
        ).input_amount
        assert v.short_cover_cost != merged

        by_pair = {g.key.group_id: g for g in _cover_groups(v)}
        assert by_pair[a.id].value == ka
        assert by_pair[b.id].value == kb


@pytest.mark.parametrize("gold,foreign,buy_fee,debt", [
    # required_net = G*q/(F-q) overflows the Numeric(16,6) pool storage (q just below F)
    ("1000000000", "1000000000", "0", "999999999"),
    # buy fee == 1 leaves no net input: unquotable
    ("100", "100", "1", "10"),
])
async def test_unquotable_short_blocks_without_zero_liability(gold, foreign, buy_fee, debt):
    async with async_session_maker() as session:
        user = await _user(session, cash="500")
        pair = await _pair(session, gold=gold, foreign=foreign, buy_fee=buy_fee)
        await _short(session, user, pair, principal=debt)

        v = await value_user_detailed(session, user.id, daily_rate=RATE)
        assert v.short_cover_cost is None
        assert v.liquidation_equity is None
        assert v.risk_basis is None
        assert v.risk_status == "blocked"
        assert v.blocked_reason == "short_quote_failed"
        assert _cover_groups(v)[0].value is None


async def test_unknown_pair_poisons_total_k_while_other_groups_stay_finite():
    """One unquotable pair makes total K/E/B unknown; it must not be dropped."""
    async with async_session_maker() as session:
        user = await _user(session, cash="1000")
        known = await _pair(session, gold="100", foreign="100")
        bad = await _pair(session, gold="100", foreign="10")
        await _short(session, user, known, principal="5")
        await _short(session, user, bad, principal="10")

        v = await value_user_detailed(session, user.id, daily_rate=RATE)
        assert v.short_cover_cost is None
        assert v.liquidation_equity is None
        assert v.risk_basis is None
        assert v.risk_status == "blocked"
        assert v.blocked_reason == "insufficient_pool_foreign"

        by_pair = {g.key.group_id: g for g in _cover_groups(v)}
        assert len(by_pair) == 2
        assert by_pair[known.id].value is not None   # known pair keeps its K
        assert by_pair[bad.id].value is None         # unknown is never zeroed
        # Reserves are valid for both, so a sourced marginal display estimate
        # still exists even though the executable K is unknown.
        assert v.short_marginal_debt is not None


async def test_display_equity_subtracts_sourced_marginal_not_executable_cover():
    """Display net worth uses the sourced marginal debt, never the executable K."""
    async with async_session_maker() as session:
        user = await _user(session, cash="1000", debt="100")
        short_pair = await _pair(session, gold="100", foreign="100", buy_fee="0.05")
        asset_pair = await _pair(session, gold="120", foreign="80")
        session.add(FxWallet(user_id=user.id, pair_id=asset_pair.id,
                             foreign_amount=Decimal("3"), cost_basis=ZERO))
        await session.commit()
        row = await _short(session, user, short_pair, principal="10")

        v = await value_user_detailed(session, user.id, daily_rate=RATE)

        q = pending_short_debt(row, RATE, datetime.now(timezone.utc))
        marginal = (q * marginal_price(Decimal("100"), Decimal("100"))).quantize(Q6)
        k = quote_buy_exact_out(
            q, Decimal("100"), Decimal("100"), Decimal("0.05"),
        ).input_amount
        mtm = (Decimal("3") * marginal_price(
            Decimal("120"), Decimal("80"),
        )).quantize(Q6)

        # Sourced display estimate and executable cover are deliberately different.
        assert marginal < k
        assert v.short_cover_cost == k
        assert v.short_marginal_debt == marginal

        expected = (
            Decimal("1000") + mtm - Decimal("100") - marginal
        ).quantize(Q6)
        assert v.display_equity == expected
        # Subtracting K instead of the sourced marginal, or dropping the
        # marginal entirely, would silently misstate net worth.
        assert v.display_equity != (
            Decimal("1000") + mtm - Decimal("100") - k
        ).quantize(Q6)
        assert v.display_equity != (
            Decimal("1000") + mtm - Decimal("100")
        ).quantize(Q6)


async def test_unsourced_marginal_makes_display_equity_unknown(monkeypatch):
    """Any unsourced short marginal poisons display for the whole short book."""
    async with async_session_maker() as session:
        user = await _user(session, cash="1000")
        good = await _pair(session, gold="100", foreign="100")
        bad = await _pair(session, gold="100", foreign="100")
        await _short(session, user, good, principal="5")
        await _short(session, user, bad, principal="7")

        real_quote = valuation_mod.quote_fx_short_group

        def poisoned_quote(pair, *, foreign_debt):
            if pair.pair_id == bad.id:
                pair = replace(pair, gold_reserve=ZERO)
            return real_quote(pair, foreign_debt=foreign_debt)

        monkeypatch.setattr(valuation_mod, "quote_fx_short_group", poisoned_quote)

        v = await value_user_detailed(session, user.id, daily_rate=RATE)

        assert v.risk_status == "blocked"
        assert v.blocked_reason == "invalid_short_reserve"
        assert v.short_cover_cost is None
        assert v.short_marginal_debt is None
        assert v.display_equity is None
        assert v.liquidation_equity is None

        # The valid pair still gets its own finite cover cost; the invalid one
        # is never zeroed.
        by_pair = {g.key.group_id: g for g in _cover_groups(v)}
        assert by_pair[good.id].value == quote_buy_exact_out(
            Decimal("5"), Decimal("100"), Decimal("100"), ZERO,
        ).input_amount
        assert by_pair[bad.id].value is None


async def test_no_short_account_keeps_old_values_and_neutral_short_fields():
    _enable_unified("2", "0.2")  # Historical account profile, not fresh-install defaults.
    async with async_session_maker() as session:
        user = await _user(session, cash="5", debt="0")
        pair = await _pair(session, gold="120", foreign="80")
        session.add(FxWallet(user_id=user.id, pair_id=pair.id,
                             foreign_amount=Decimal("3"), cost_basis=ZERO))
        await session.commit()

        v = await value_user_detailed(session, user.id, daily_rate=RATE)
        expected_asset = quote_sell(
            Decimal("3"), Decimal("120"), Decimal("80"), ZERO,
        ).output_amount
        mtm = (Decimal("3") * marginal_price(
            Decimal("120"), Decimal("80"),
        )).quantize(Q6)

        assert v.mtm_fx == mtm
        assert v.display_equity == (Decimal("5") + mtm).quantize(Q6)
        assert v.liquidation_equity == (Decimal("5") + expected_asset).quantize(Q6)
        assert v.groups[0].role == "asset_sale"
        assert v.groups[0].value == expected_asset
        assert v.groups[0].executable is True

        # No short: K is a known zero, not unknown; locks are zero.
        assert v.short_cover_cost == ZERO
        assert v.short_marginal_debt == ZERO
        assert v.restricted_cash == ZERO
        assert v.available_cash == Decimal("5")
        assert v.risk_status == "ok" and v.blocked_reason is None
        # Debt-free assets still contribute to the risk basis of the 2x profile.
        assert v.risk_basis == (Decimal("0.5") * expected_asset).quantize(Q6, rounding=ROUND_CEILING)


async def test_risk_basis_uses_max_debt_or_alpha_assets_plus_alpha_short():
    _enable_unified("10", "0.05")  # alpha = (10-1)/10 = 0.9
    alpha = Decimal("0.9")
    async with async_session_maker() as session:
        # Debt-dominant account: max(D, alpha*A) must pick D.
        user = await _user(session, cash="1000", debt="4000")
        short_pair = await _pair(session, gold="100", foreign="100")
        asset_pair = await _pair(session, gold="200", foreign="100")
        session.add(FxWallet(user_id=user.id, pair_id=asset_pair.id,
                             foreign_amount=Decimal("1"), cost_basis=ZERO))
        await session.commit()
        await _short(session, user, short_pair, principal="5")

        v = await value_user_detailed(session, user.id, daily_rate=RATE)
        ka = quote_buy_exact_out(Decimal("5"), Decimal("100"), Decimal("100"), ZERO).input_amount
        a = quote_sell(Decimal("1"), Decimal("200"), Decimal("100"), ZERO).output_amount
        expected_e = (Decimal("1000") + a - Decimal("4000") - ka).quantize(Q6)
        expected_b = (max(Decimal("4000"), alpha * a) + alpha * ka).quantize(
            Q6, rounding=ROUND_CEILING,
        )
        assert v.short_cover_cost == ka
        assert v.liquidation_equity == expected_e
        assert v.risk_basis == expected_b
        # Sanity: alpha*A is far below D here, so the debt term dominates.
        assert alpha * a < Decimal("4000")

        # Asset-dominant account: max(D, alpha*A) must pick alpha*A.
        user2 = await _user(session, cash="1000", debt="1")
        short_pair2 = await _pair(session, gold="100", foreign="100")
        asset_pair2 = await _pair(session, gold="20000", foreign="100")
        session.add(FxWallet(user_id=user2.id, pair_id=asset_pair2.id,
                             foreign_amount=Decimal("10"), cost_basis=ZERO))
        await session.commit()
        await _short(session, user2, short_pair2, principal="5")

        v2 = await value_user_detailed(session, user2.id, daily_rate=RATE)
        a2 = quote_sell(Decimal("10"), Decimal("20000"), Decimal("100"), ZERO).output_amount
        assert alpha * a2 > Decimal("1")
        expected_b2 = (max(Decimal("1"), alpha * a2) + alpha * ka).quantize(
            Q6, rounding=ROUND_CEILING,
        )
        assert v2.risk_basis == expected_b2


async def test_short_valuation_batches_sql_for_many_users():
    async with async_session_maker() as session:
        pair = await _pair(session, gold="100", foreign="100")
        users = [
            User(username=f"wp2b1-bulk-{i}", cash=Decimal("10"))
            for i in range(100)
        ]
        session.add_all(users)
        await session.commit()
        session.add_all([
            FxShortPosition(
                user_id=user.id, pair_id=pair.id,
                principal_foreign=Decimal("1"), interest_foreign=ZERO,
                restricted_gold=Decimal("1"), proceeds_basis_gold=Decimal("1"),
                interest_last_accrued_at=datetime.now(timezone.utc),
            )
            for user in users
        ])
        await session.commit()

        statements: list[str] = []

        def _capture(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", _capture)
        try:
            first = await value_users_batch(
                session, [users[0].id], daily_rate=RATE,
            )
            first_count = len(statements)
            statements.clear()
            second = await value_users_batch(
                session, [user.id for user in users], daily_rate=RATE,
            )
            second_count = len(statements)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", _capture)

        assert len(first) == 1 and len(second) == 100
        # Detect per-account queries without fixing the number of batched reads.
        assert second_count <= first_count, statements
        assert all(v.short_cover_cost is not None for v in second.values())


async def test_materialized_short_projection_keeps_same_clock_and_reference_cost():
    from app.services.credit.account_read import build_short_positions
    _enable_unified()
    clock = datetime(2026, 10, 1, tzinfo=timezone.utc)
    async with async_session_maker() as session:
        user = await _user(session, cash='100')
        pair = await _pair(session)
        row = await _short(session, user, pair, principal='10', accrued=clock)
        value = await value_user_detailed(session, user.id, daily_rate=RATE)
        positions = build_short_positions(value, fx_enabled=False, unified_enabled=True)
        assert len(positions) == 1
        assert sum((p.reference_cover_cost for p in positions), ZERO) == value.short_cover_cost
        assert all(p.blocked_reason == 'fx_disabled' and not p.executable for p in positions)
        assert all(p.risk_status == 'ok' for p in positions)
        assert positions[0].pending_short_debt == value.short_positions[0].pending_short_debt
        await session.refresh(row)
        assert row.interest_last_accrued_at.replace(tzinfo=timezone.utc) == clock
