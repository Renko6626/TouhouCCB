"""WP2b2: transaction-after shared credit risk for FX short debt.

These tests assert actual risk decisions, equity/cover amounts and unchanged
persisted money for fabricated ``FxShortPosition`` rows.  They deliberately do
not assert source text, helper names or cache layout: a regression here means a
wrong allow/deny, a foreign-only account wrongly given a fast path, a locked
short proceeds total counted twice, a stale post-order reserve/K silently
accepted, or an unknown cover cost treated as zero.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import ROUND_FLOOR, Decimal

import pytest
from sqlalchemy import select

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.database import async_session_maker
from app.models.base import Market, Outcome, Position, SiteConfig, User
from app.models.fx import FxPair, FxShortPosition
from app.services.credit import flags as credit_flags
from app.services.credit import risk
from app.services.credit.keys import GroupKey
from app.services.credit.risk import (
    REASON_INSUFFICIENT_INITIAL_MARGIN,
    REASON_VERSION_CONFLICT,
    DependencySet,
    PostTradeState,
    check_cash_spend,
    check_new_risk,
    clear_risk_cache,
    discover_dependencies,
)
from app.services.credit.thresholds import derive_thresholds
from app.services.fx.amm import quote_buy_exact_out
from app.services.fx.shorts import pending_short_debt

pytestmark = pytest.mark.asyncio

ZERO = Decimal("0")
ONE = Decimal("1")
Q6 = Decimal("0.000001")
RATE = Decimal("0.01")
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
TH4 = derive_thresholds(Decimal("4"), Decimal("0.1"))     # r_initial = 1/3, alpha = 0.75
TH10 = derive_thresholds(Decimal("10"), Decimal("0.05"))  # r_initial = 1/9, alpha = 0.9


@pytest.fixture(autouse=True)
def _clean_risk_state():
    credit_flags.clear_flags()
    risk.set_risk_cache_size(risk.DEFAULT_CACHE_SIZE)
    clear_risk_cache()
    yield
    credit_flags.clear_flags()
    clear_risk_cache()
    risk.set_risk_cache_size(risk.DEFAULT_CACHE_SIZE)


# ────────────────────────────── seed helpers ──────────────────────────────

async def _seed_config(**kv: str) -> None:
    async with async_session_maker() as s:
        async with s.begin():
            for key, value in kv.items():
                s.add(SiteConfig(key=key, value=str(value), value_type="string"))


async def _user(s, name, *, cash="0", debt="0", accrued=None, version=0) -> User:
    user = User(
        username=name, cash=Decimal(cash), debt=Decimal(debt),
        debt_last_accrued_at=accrued, economic_version=version,
    )
    s.add(user)
    await s.commit()
    await s.refresh(user)
    return user


async def _market(s, title, shares, *, status="trading", b=100.0):
    market = Market(title=title, liquidity_b=b, status=status)
    s.add(market)
    await s.flush()
    outcomes = [
        Outcome(market_id=market.id, label=f"o{i}", total_shares=Decimal(str(amount)))
        for i, amount in enumerate(shares)
    ]
    s.add_all(outcomes)
    await s.commit()
    for outcome in outcomes:
        await s.refresh(outcome)
    await s.refresh(market)
    return market, outcomes


async def _position(s, user, outcome, amount) -> None:
    s.add(Position(user_id=user.id, outcome_id=outcome.id,
                   amount=Decimal(str(amount)), cost_basis=ZERO))
    await s.commit()


async def _pair(s, code, *, gold="1000", foreign="1000", status="trading",
                reduce_only=False, buy_fee="0", sell_fee="0") -> FxPair:
    pair = FxPair(
        currency_code=code, currency_name=code, status=status, reduce_only=reduce_only,
        gold_reserve=Decimal(gold), foreign_reserve=Decimal(foreign),
        buy_fee_rate=Decimal(buy_fee), sell_fee_rate=Decimal(sell_fee),
    )
    s.add(pair)
    await s.commit()
    await s.refresh(pair)
    return pair


async def _short(s, user, pair, *, principal="0", interest="0", restricted="0",
                 accrued=None) -> FxShortPosition:
    total = Decimal(principal) + Decimal(interest)
    if accrued is None and total > 0:
        accrued = NOW
    row = FxShortPosition(
        user_id=user.id, pair_id=pair.id,
        principal_foreign=Decimal(principal), interest_foreign=Decimal(interest),
        restricted_gold=Decimal(restricted), proceeds_basis_gold=Decimal(restricted),
        interest_last_accrued_at=accrued,
    )
    s.add(row)
    await s.commit()
    await s.refresh(row)
    return row


async def _deps(uid: int):
    async with async_session_maker() as s:
        return await discover_dependencies(s, uid)


async def _check(uid: int, *, post=None, thresholds=TH10, deps=None):
    async with async_session_maker() as s:
        user = (await s.execute(select(User).where(User.id == uid))).scalars().one()
        current = deps if deps is not None else await discover_dependencies(s, uid)
        if post is None:
            post = PostTradeState(cash=current.cash, debt=current.debt)
        return await check_new_risk(
            s, user=user, deps=current, post=post, thresholds=thresholds,
            partial_pct=ONE, now=NOW,
        )


async def _check_spend(uid: int, spend, *, thresholds=TH4, deps=None):
    async with async_session_maker() as s:
        user = (await s.execute(select(User).where(User.id == uid))).scalars().one()
        current = deps if deps is not None else await discover_dependencies(s, uid)
        return await check_cash_spend(
            s, user=user, deps=current, spend=Decimal(str(spend)),
            thresholds=thresholds, partial_pct=ONE, now=NOW,
        )


async def _user_money(uid: int) -> tuple[Decimal, Decimal]:
    async with async_session_maker() as s:
        user = (await s.execute(select(User).where(User.id == uid))).scalars().one()
        return Decimal(user.cash), Decimal(user.debt)


async def _short_row(uid: int, pid: int) -> tuple:
    async with async_session_maker() as s:
        row = (await s.execute(select(FxShortPosition).where(
            FxShortPosition.user_id == uid, FxShortPosition.pair_id == pid,
        ))).scalars().one()
        return (row.principal_foreign, row.interest_foreign,
                row.interest_last_accrued_at, row.restricted_gold)


# ────────────────────────────── dependency discovery ──────────────────────────────

async def test_short_pair_is_a_dependency_without_wallet_or_gold_debt():
    """A foreign-only borrower must still appear in groups and complete snapshots."""
    async with async_session_maker() as s:
        user = await _user(s, "disc_short", cash="500", debt="0")
        pair = await _pair(s, "disc_short_p", gold="1000", foreign="2000",
                           buy_fee="0.01", sell_fee="0.02")
        row = await _short(s, user, pair, principal="10", restricted="0")
        uid, pid, accrued = int(user.id), int(pair.id), row.interest_last_accrued_at

    deps = await _deps(uid)
    key = GroupKey("fx", pid)
    assert key in deps.groups
    assert deps.holdings.get(key, {}) == {}     # no positive wallet
    assert deps.debt == ZERO                    # no gold debt
    snap = deps.snapshots[key]
    assert snap.short_pair is not None
    assert snap.short_pair.buy_fee_rate == Decimal("0.01")
    assert snap.short_pair.sell_fee_rate == Decimal("0.02")
    assert snap.short_pair.status == "trading"
    assert snap.pool_version is not None
    assert snap.short_debt is not None
    assert snap.short_debt.principal_foreign == Decimal("10")
    assert snap.short_debt.interest_last_accrued_at == accrued


async def test_cash_spend_with_minimal_deps_rejects_ungated_persisted_short():
    """A minimal deps set (gold debt 0) must not hide a foreign obligation.

    The core may not add the missing pair's GATE after the User lock and quote
    it under ungated reserves.  The conservative answer is a stable
    ``version_conflict`` so the caller releases, rediscovers with the short pair
    in ``extra_groups`` and retries; neither spend is allowed and no money moves.
    """
    await _seed_config(loan_daily_rate="0.01")
    async with async_session_maker() as s:
        user = await _user(s, "min_deps", cash="5000", debt="0")
        pair = await _pair(s, "min_deps_p", gold="1000", foreign="1000")
        row = await _short(s, user, pair, principal="10")
        uid, pid = int(user.id), int(pair.id)
        before = (row.principal_foreign, row.interest_foreign,
                  row.interest_last_accrued_at, row.restricted_gold)

    async with async_session_maker() as s:
        row = (await s.execute(select(User).where(User.id == uid))).scalars().one()
        minimal = DependencySet(
            economic_version=int(row.economic_version or 0),
            cash=Decimal("5000"), debt=ZERO, debt_last_accrued_at=None,
            groups=(), holdings={},
        )
        # Even a spend that the short margin would easily swallow must not be
        # admitted from an ungated pair; the obligation is never silently 0.
        big = await check_cash_spend(
            s, user=row, deps=minimal, spend=Decimal("4999"),
            thresholds=TH4, partial_pct=ONE, now=NOW,
        )
        small = await check_cash_spend(
            s, user=row, deps=minimal, spend=Decimal("1"),
            thresholds=TH4, partial_pct=ONE, now=NOW,
        )
    for decision in (big, small):
        assert decision.allowed is False
        assert decision.reason == REASON_VERSION_CONFLICT
        # Never a K from an ungated added pair.
        assert decision.short_cover_cost is None
        assert decision.max_borrow is None
    assert await _user_money(uid) == (Decimal("5000"), Decimal("0"))
    assert await _short_row(uid, pid) == before


# ────────────────────────────── (1) foreign-only blocks new risk ──────────────────────────────

async def test_foreign_only_short_blocks_gold_loan_and_cash_spend():
    await _seed_config(loan_daily_rate="0.01")
    async with async_session_maker() as s:
        borrower = await _user(s, "fo_borrower", cash="5000", debt="0")
        control = await _user(s, "fo_control", cash="5000", debt="0")
        pair = await _pair(s, "fo_p", gold="1000", foreign="1000")
        row = await _short(s, borrower, pair, principal="10", restricted="100")
        bid, cid, pid = int(borrower.id), int(control.id), int(pair.id)
        before = (row.principal_foreign, row.interest_foreign,
                  row.interest_last_accrued_at, row.restricted_gold)

    k = quote_buy_exact_out(Decimal("10"), Decimal("1000"), Decimal("1000"), ZERO).input_amount

    # 3*C = 15000 without a short; the short shrinks headroom by ~3.75*K.
    loan = Decimal("14999")
    post = PostTradeState(cash=Decimal("5000") + loan, debt=loan)
    with_short = await _check(bid, post=post, thresholds=TH4)
    without_short = await _check(cid, post=post, thresholds=TH4)

    assert without_short.allowed is True, without_short
    assert with_short.allowed is False, with_short
    assert with_short.reason == REASON_INSUFFICIENT_INITIAL_MARGIN
    assert with_short.debt_after == loan
    assert with_short.short_cover_cost == k
    assert with_short.max_borrow == ZERO
    # Rejection must not have moved persisted money or the short row.
    assert await _user_money(bid) == (Decimal("5000"), Decimal("0"))
    assert await _short_row(bid, pid) == before

    # Cash spend that would breach the short margin is denied on the same basis.
    spend_ok = await _check_spend(bid, Decimal("5000") - Decimal("1.25") * k - ONE)
    spend_bad = await _check_spend(bid, Decimal("5000") - Decimal("1.25") * k + ONE)
    assert spend_ok.allowed is True, spend_ok
    assert spend_bad.allowed is False, spend_bad
    assert spend_bad.reason == REASON_INSUFFICIENT_INITIAL_MARGIN
    assert await _user_money(bid) == (Decimal("5000"), Decimal("0"))


# ────────────────────────────── (2) mixed long/short W boundary ──────────────────────────────

async def test_mixed_long_short_boundary_counts_cash_only_once():
    await _seed_config(loan_daily_rate="0.01", sell_fee_rate="0.0")
    restricted = Decimal("1000")
    async with async_session_maker() as s:
        user = await _user(s, "mixed", cash="100000", debt="0")
        market, outcomes = await _market(s, "mixed_m", [100, 100])
        await _position(s, user, outcomes[0], 40)
        pair = await _pair(s, "mixed_p", gold="1000", foreign="1000")
        await _short(s, user, pair, principal="50", restricted=str(restricted))
        uid, pid = int(user.id), int(pair.id)

    deps = await _deps(uid)
    probe = await _check(uid, deps=deps, thresholds=TH10)
    assert probe.allowed is True, probe
    k = probe.short_cover_cost
    assert k is not None and k > ZERO
    # A = E + D + K - C from the probe's own full-portfolio valuation.
    a = probe.equity_after - deps.cash + probe.debt_after + k

    # Pick a debt that dominates alpha*A so B = D + alpha*K.
    alpha = TH10.alpha
    debt = (alpha * a).to_integral_value(rounding=ROUND_FLOOR) + Decimal("10000")
    assert debt > alpha * a
    basis, w = TH10.basis_measure(
        debt=debt, positive_assets=a, short_cover=k,
    )
    assert (w / (TH10.leverage * (TH10.leverage - ONE))).is_finite()
    boundary_cash = debt + k + basis / (TH10.leverage - ONE) - a

    at = await _check(uid, deps=deps, thresholds=TH10, post=PostTradeState(
        cash=boundary_cash + Q6, debt=debt,
    ))
    below = await _check(uid, deps=deps, thresholds=TH10, post=PostTradeState(
        cash=boundary_cash - Q6, debt=debt,
    ))
    assert at.allowed is True, at
    assert below.allowed is False, below
    assert below.reason == REASON_INSUFFICIENT_INITIAL_MARGIN

    # C already contains the locked short proceeds S: the decision uses C once.
    assert at.equity_after == (boundary_cash + Q6 + a - debt - k).quantize(Q6)
    assert at.equity_after != (boundary_cash + Q6 + restricted + a - debt - k).quantize(Q6)
    assert below.max_borrow == ZERO


async def test_new_short_open_post_state_is_risk_checked_without_persisted_row():
    """A short open passed only through PostTradeState is fully valued, no second model."""
    await _seed_config(loan_daily_rate="0.01")
    async with async_session_maker() as s:
        user = await _user(s, "open_new", cash="100", debt="0")
        pair = await _pair(s, "open_new_p", gold="1000", foreign="1000")
        uid, pid = int(user.id), int(pair.id)

    deps = await _deps(uid)
    assert GroupKey("fx", pid) not in deps.groups     # nothing persisted yet

    q = Decimal("10")
    proceeds = Decimal("10")
    decision = await _check(uid, deps=deps, post=PostTradeState(
        cash=Decimal("100") + proceeds, debt=ZERO,
        short_debt={pid: q},
        short_reserves={pid: (Decimal("1010"), Decimal("990"))},
    ))
    k = quote_buy_exact_out(q, Decimal("1010"), Decimal("990"), ZERO).input_amount
    assert decision.short_debt_after == q
    assert decision.short_cover_cost == k
    assert decision.equity_after == (Decimal("110") - k).quantize(Q6)
    # Ignoring the new short (K=0) would have produced a different, wrong equity.
    assert decision.equity_after != Decimal("110.000000")


# ────────────────────────────── (3) post reserves / interest move K ──────────────────────────────

async def test_post_reserve_and_pending_interest_change_k_and_admission():
    await _seed_config(loan_daily_rate="0.01")
    async with async_session_maker() as s:
        base_user = await _user(s, "k_base", cash="100000", debt="0")
        int_user = await _user(s, "k_interest", cash="100000", debt="0")
        plain = await _pair(s, "k_plain", gold="1000", foreign="1000")
        old = await _pair(s, "k_old", gold="1000", foreign="1000")
        base_row = await _short(s, base_user, plain, principal="10", restricted="50")
        int_row = await _short(s, int_user, old, principal="10", restricted="50",
                               accrued=NOW - timedelta(days=30))
        buid, iuid, pid = int(base_user.id), int(int_user.id), int(plain.id)
        base_before = await _short_row(buid, pid)
        int_before = await _short_row(iuid, int(old.id))

    k0 = quote_buy_exact_out(Decimal("10"), Decimal("1000"), Decimal("1000"), ZERO).input_amount
    debt = Decimal("3000")
    basis0, _ = TH4.basis_measure(debt=debt, positive_assets=ZERO, short_cover=k0)
    boundary = debt + k0 + basis0 / (TH4.leverage - ONE)
    allowed = await _check(buid, thresholds=TH4, post=PostTradeState(
        cash=boundary + Q6, debt=debt,
    ))
    assert allowed.allowed is True, allowed
    assert allowed.short_cover_cost == k0

    # Same order, but the simulated post-order reserves are thinner -> bigger K.
    changed = await _check(buid, thresholds=TH4, post=PostTradeState(
        cash=boundary + Q6, debt=debt,
        short_reserves={pid: (Decimal("1000"), Decimal("990"))},
    ))
    assert changed.allowed is False, changed
    assert changed.reason == REASON_INSUFFICIENT_INITIAL_MARGIN
    assert changed.short_cover_cost > k0
    assert await _short_row(buid, pid) == base_before

    # 30 days of pending foreign interest raises the debt and K past the boundary.
    stale = await _check(iuid, thresholds=TH4, post=PostTradeState(
        cash=boundary + Q6, debt=debt,
    ))
    assert stale.allowed is False, stale
    assert stale.short_cover_cost > k0
    assert await _short_row(iuid, int(old.id)) == int_before

    # Sanity: the interest row really is worth more than its principal.
    async with async_session_maker() as s:
        row = (await s.execute(select(FxShortPosition).where(
            FxShortPosition.user_id == iuid))).scalars().one()
        assert pending_short_debt(row, RATE, NOW) > Decimal("10")


# ────────────────────────────── (4) unknown K vs paused finite K ──────────────────────────────
async def test_unknown_pool_and_paused_market_are_distinct_blocked_reasons():
    await _seed_config(loan_daily_rate="0.01")
    async with async_session_maker() as s:
        unknown_user = await _user(s, "unknown_k", cash="100000", debt="0")
        paused_user = await _user(s, "paused_k", cash="100000", debt="0")
        thin = await _pair(s, "thin_p", gold="100", foreign="10")
        paused = await _pair(s, "paused_p", gold="100", foreign="100",
                             status="paused", reduce_only=False)
        await _short(s, unknown_user, thin, principal="10")
        await _short(s, paused_user, paused, principal="10")
        uuid_, puid = int(unknown_user.id), int(paused_user.id)

    unknown = await _check(uuid_)
    paused_dec = await _check(puid)

    assert unknown.allowed is False
    assert unknown.reason == "insufficient_pool_foreign"
    assert unknown.equity_after is None
    assert unknown.risk_basis is None
    # Theoretical headroom is zero when K is unknown (never a negative/None loan).
    assert unknown.max_borrow == ZERO
    assert unknown.short_cover_cost is None

    assert paused_dec.allowed is False
    assert paused_dec.reason == "pair_paused"
    assert paused_dec.equity_after is not None
    assert paused_dec.short_cover_cost is not None
    assert paused_dec.max_borrow == ZERO

    assert unknown.reason != paused_dec.reason
    assert unknown.equity_after is not paused_dec.equity_after


# ────────────────────────────── (5) no-short equivalence / explicit clear ──────────────────────────────

async def test_explicit_zero_short_clears_to_old_no_short_decision():
    await _seed_config(loan_daily_rate="0.01")
    async with async_session_maker() as s:
        plain = await _user(s, "clear_plain", cash="100", debt="300")
        shorter = await _user(s, "clear_short", cash="100", debt="300")
        pair = await _pair(s, "clear_p", gold="1000", foreign="1000")
        await _short(s, shorter, pair, principal="10", restricted="40")
        pid = int(pair.id)
        plain_uid, short_uid = int(plain.id), int(shorter.id)

    no_short = await _check(plain_uid)
    assert no_short.allowed is False
    assert no_short.reason == REASON_INSUFFICIENT_INITIAL_MARGIN
    # Old no-short comparison still decides the no-short account exactly.
    assert no_short.allowed == (no_short.equity_after >= TH10.r_initial * no_short.debt_after)

    cleared = await _check(short_uid, post=PostTradeState(
        cash=Decimal("100"), debt=Decimal("300"), short_debt={pid: ZERO},
    ))
    assert cleared.allowed == no_short.allowed
    assert cleared.equity_after == no_short.equity_after
    assert cleared.debt_after == no_short.debt_after


# ────────────────────────────── (6) stale post-order reserves deny ──────────────────────────────

async def test_stale_post_order_short_reserve_is_not_used():
    await _seed_config(loan_daily_rate="0.01")
    async with async_session_maker() as s:
        user = await _user(s, "stale_short", cash="100000", debt="0")
        pair = await _pair(s, "stale_short_p", gold="1000", foreign="1000")
        await _short(s, user, pair, principal="10", restricted="50")
        uid, pid = int(user.id), int(pair.id)

    deps = await _deps(uid)
    key = GroupKey("fx", pid)
    base_version = deps.snapshots[key].version

    # Concurrently move the pool; the caller's simulated post-order reserves
    # are now based on an older version and must be rejected, not valued.
    async with async_session_maker() as s:
        async with s.begin():
            row = (await s.execute(select(FxPair).where(FxPair.id == pid))).scalars().one()
            row.gold_reserve = Decimal("800")

    decision = await _check(uid, deps=deps, post=PostTradeState(
        cash=deps.cash, debt=deps.debt,
        short_reserves={pid: (Decimal("1200"), Decimal("1000"))},
        base_versions={key: base_version},
    ))
    assert decision.allowed is False
    assert decision.reason == REASON_VERSION_CONFLICT


async def test_concurrent_new_short_set_returns_conflict_not_ungated_quote():
    """A persisted short that appeared after discovery must not be priced under
    the already-held (incomplete) gate set; the core returns ``version_conflict``
    and the caller releases, rediscovers and retries with the pair gated."""
    await _seed_config(loan_daily_rate="0.01")
    async with async_session_maker() as s:
        user = await _user(s, "late_short", cash="100000", debt="0")
        pair = await _pair(s, "late_short_p", gold="1000", foreign="1000")
        pid = int(pair.id)
        uid = int(user.id)

    deps = await _deps(uid)                      # discovery sees no short yet
    assert GroupKey("fx", pid) not in deps.groups

    # Concurrent short open commits without the discovery snapshot noticing.
    async with async_session_maker() as s:
        async with s.begin():
            s.add(FxShortPosition(
                user_id=uid, pair_id=pid,
                principal_foreign=Decimal("10"), interest_foreign=ZERO,
                restricted_gold=Decimal("50"), proceeds_basis_gold=Decimal("50"),
                interest_last_accrued_at=NOW,
            ))

    decision = await _check(uid, deps=deps, post=PostTradeState(
        cash=deps.cash, debt=deps.debt,
    ))
    assert decision.allowed is False
    assert decision.reason == REASON_VERSION_CONFLICT
    assert decision.short_cover_cost is None
    assert decision.max_borrow is None
    # No money changed and the concurrently opened short row is untouched.
    assert await _user_money(uid) == (Decimal("100000"), Decimal("0"))
    principal, interest, accrued, restricted = await _short_row(uid, pid)
    assert (principal, interest, restricted) == (Decimal("10"), ZERO, Decimal("50"))
    assert accrued is not None


async def test_revalidation_new_asset_group_is_rejected_not_priced():
    """If version revalidation discovers a new holding group, the caller's gate
    set from discovery is incomplete.  The core must reject rather than price
    that group's collateral under a gate it never held."""
    await _seed_config(loan_daily_rate="0.01")
    async with async_session_maker() as s:
        user = await _user(s, "reval_group", cash="5000", debt="100")
        _, outcomes = await _market(s, "reval_g_m1", [100, 100])
        other, other_outcomes = await _market(s, "reval_g_m2", [100, 100])
        await _position(s, user, outcomes[0], 40)
        uid = int(user.id)
        other_key = GroupKey("lmsr", int(other.id))
        other_outcome = int(other_outcomes[0].id)

    async with async_session_maker() as s1:
        stale_user = (await s1.execute(
            select(User).where(User.id == uid)
        )).scalars().one()
        await s1.commit()                        # expire_on_commit=False → stale attrs
        deps = await discover_dependencies(s1, uid)
        assert other_key not in deps.groups
        assert other_key not in deps.snapshots

        # Concurrent writer opens a new holding group and bumps our version.
        async with async_session_maker() as s2:
            async with s2.begin():
                s2.add(Position(user_id=uid, outcome_id=other_outcome,
                                amount=Decimal("40"), cost_basis=ZERO))
                row = (await s2.execute(
                    select(User).where(User.id == uid)
                )).scalars().one()
                row.economic_version = int(stale_user.economic_version or 0) + 1

        decision = await check_new_risk(
            s1, user=stale_user, deps=deps,
            post=PostTradeState(cash=deps.cash, debt=deps.debt),
            thresholds=TH10, partial_pct=ONE, now=NOW,
        )
        assert decision.allowed is False
        assert decision.reason == REASON_VERSION_CONFLICT
        assert decision.short_cover_cost is None
