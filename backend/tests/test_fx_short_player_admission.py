"""WP2b3b: player loan and spot-buy adapters under foreign-only short risk.

These are persisted integration scenarios against the real public write paths
(``fx.trading.execute_trade``, the LMSR market writer / legacy ``buy_shares``,
and the loan quota/borrow endpoints).  They assert actual cash, wallet, pool,
treasury, debt and position postconditions, never source text or helper names.

A regression here means a foreign-only account (``User.debt == 0`` but a live
``FxShortPosition``) can buy FX/LMSR or borrow gold on the old gold-only fast
path, a same-pair spot buy silently opens a long beside an outstanding short,
or the loan quota 500s / reports gold-only headroom when the short cover cost is
unknown.
"""
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.base import Market, MarketStatus, Outcome, Position, SiteConfig, User
from app.models.fx import FxPair, FxShortPosition, FxTrade, FxTreasury, FxWallet
from app.services import site_config
from app.services.credit import flags as credit_flags
from app.services.credit.flags import CreditFlags
from app.services.credit.gates import GATES
from app.services.credit.ownership import OWNERSHIP
from app.services.credit.thresholds import derive_thresholds
from app.services.fx import trading
from app.services.fx.amm import quote_buy_exact_out
from app.api.v1.loan import borrow, get_quota
from app.schemas.loan import BorrowRequest

pytestmark = pytest.mark.asyncio

ZERO = D("0")
UNIFIED = CreditFlags(
    unified_credit_enabled=True,
    credit_leverage=D("4"),
    credit_maintenance_ratio=D("0.1"),
)
TH4 = derive_thresholds(D("4"), D("0.1"))  # r_initial = 1/3, alpha = 0.75


@pytest.fixture(autouse=True)
def _unified(monkeypatch):
    credit_flags.clear_flags()
    credit_flags.set_flags(UNIFIED)
    monkeypatch.setattr(OWNERSHIP, "_writes_enabled", True)
    site_config.clear_cache()
    yield
    credit_flags.clear_flags()
    site_config.clear_cache()


async def _user(db, *, name: str, cash: str = "100", debt: str = "0") -> User:
    user = User(username=name, cash=D(cash), debt=D(debt),
                tos_accepted_at=datetime.now(timezone.utc))
    db.add(user)
    await db.flush()
    return user


async def _pair(db, *, code: str, gold: str = "1000", foreign: str = "1000",
                buy_fee: str = "0", sell_fee: str = "0",
                status: str = "trading", reduce_only: bool = False) -> FxPair:
    pair = FxPair(currency_code=code, currency_name=code, status=status,
                  reduce_only=reduce_only,
                  gold_reserve=D(gold), foreign_reserve=D(foreign),
                  buy_fee_rate=D(buy_fee), sell_fee_rate=D(sell_fee))
    db.add(pair)
    await db.flush()
    db.add(FxTreasury(pair_id=pair.id, gold_balance=D(gold), foreign_balance=D(foreign)))
    return pair


async def _short(db, *, user_id: int, pair_id: int, principal: str = "10",
                 interest: str = "0", restricted: str = "0") -> FxShortPosition:
    total = D(principal) + D(interest)
    row = FxShortPosition(
        user_id=user_id, pair_id=pair_id,
        principal_foreign=D(principal), interest_foreign=D(interest),
        restricted_gold=D(restricted), proceeds_basis_gold=D(restricted),
        interest_last_accrued_at=datetime.now(timezone.utc) if total > 0 else None,
    )
    db.add(row)
    await db.flush()
    return row


async def _config(db, **kv) -> None:
    for key, value in kv.items():
        row = (await db.execute(
            select(SiteConfig).where(SiteConfig.key == key))).scalar_one_or_none()
        if row is not None:
            row.value = str(value)
        else:
            db.add(SiteConfig(key=key, value=str(value), value_type="string"))


async def _market(db, *, title: str, b: float = 100.0):
    market = Market(title=title, liquidity_b=b, status=MarketStatus.TRADING)
    db.add(market)
    await db.flush()
    outcomes = [
        Outcome(market_id=market.id, label=label, total_shares=ZERO)
        for label in ("A", "B")
    ]
    db.add_all(outcomes)
    await db.flush()
    return market, outcomes


def _k(pair_id: int, principal: str, gold: str = "1000", foreign: str = "1000",
       buy_fee: str = "0") -> D:
    return quote_buy_exact_out(D(principal), D(gold), D(foreign), D(buy_fee)).input_amount


# ─────────────────────────── FX spot buy ───────────────────────────

async def test_foreign_only_short_blocks_other_pair_fx_buy():
    async with async_session_maker() as db:
        user = await _user(db, name="fx_short_block")
        short_pair = await _pair(db, code="FXBLKSHORT")
        target = await _pair(db, code="FXBLKTARGET")
        await _short(db, user_id=user.id, pair_id=short_pair.id, principal="500")
        await _config(db, fx_enabled="true", loan_enabled="true", loan_daily_rate="0")
        await db.commit()
        site_config.clear_cache()
        uid, spid, tpid = user.id, short_pair.id, target.id

    async with async_session_maker() as db:
        with pytest.raises(HTTPException) as exc:
            await trading.execute_trade(db, uid, tpid, "buy", D("10"), D("0"), "fx-block")
        assert exc.value.status_code == 400
        assert "insufficient_initial_margin" in str(exc.value.detail)

        assert (await db.get(User, uid)).cash == D("100")
        assert (await db.get(User, uid)).debt == ZERO
        assert (await db.execute(select(FxWallet))).scalars().all() == []
        assert (await db.execute(select(FxTrade))).scalars().all() == []
        pair = await db.get(FxPair, tpid)
        assert (pair.gold_reserve, pair.foreign_reserve) == (D("1000"), D("1000"))
        assert pair.pool_version == 1
        short = (await db.execute(select(FxShortPosition).where(
            FxShortPosition.user_id == uid, FxShortPosition.pair_id == spid,
        ))).scalars().one()
        assert short.principal_foreign == D("500") and short.restricted_gold == ZERO
        assert not GATES.held_keys()


async def test_same_pair_spot_buy_with_live_short_is_rejected_without_wallet():
    async with async_session_maker() as db:
        user = await _user(db, name="fx_same_pair", cash="100000")
        pair = await _pair(db, code="FXSAMEPAIR")
        await _short(db, user_id=user.id, pair_id=pair.id, principal="10")
        await _config(db, fx_enabled="true", loan_enabled="true", loan_daily_rate="0")
        await db.commit()
        site_config.clear_cache()
        uid, pid = user.id, pair.id

    async with async_session_maker() as db:
        with pytest.raises(HTTPException) as exc:
            await trading.execute_trade(db, uid, pid, "buy", D("1"), D("0"), "fx-cover-entry")
        assert exc.value.status_code == 400
        assert "cover" in str(exc.value.detail).lower()

        assert (await db.get(User, uid)).cash == D("100000")
        assert (await db.execute(select(FxWallet))).scalars().all() == []
        assert (await db.execute(select(FxTrade))).scalars().all() == []
        assert (await db.get(FxPair, pid)).pool_version == 1
        assert not GATES.held_keys()


async def test_healthy_mixed_short_can_buy_another_pair_with_shared_margin():
    async with async_session_maker() as db:
        user = await _user(db, name="fx_healthy", cash="100000")
        short_pair = await _pair(db, code="FXHEALTHYSHORT")
        target = await _pair(db, code="FXHEALTHYTARGET")
        await _short(db, user_id=user.id, pair_id=short_pair.id, principal="10")
        await _config(db, fx_enabled="true", loan_enabled="true", loan_daily_rate="0")
        await db.commit()
        site_config.clear_cache()
        uid, tpid = user.id, target.id

    async with async_session_maker() as db:
        public = await trading.execute_trade(db, uid, tpid, "buy", D("10"), D("0"), "fx-ok")
        assert public.output_amount > 0
        wallet = (await db.execute(select(FxWallet).where(
            FxWallet.user_id == uid, FxWallet.pair_id == tpid))).scalars().one()
        assert wallet.foreign_amount == public.output_amount
        assert (await db.get(User, uid)).cash < D("100000")
        assert not GATES.held_keys()


# ─────────────────────────── LMSR buy (writer + legacy) ───────────────────────────

async def test_foreign_only_short_blocks_lmsr_writer_buy():
    from app.services.market_writer import WRITER
    from app.services.writer_ops import BuyCmd

    async with async_session_maker() as db:
        user = await _user(db, name="lmsr_writer_short")
        short_pair = await _pair(db, code="LMSRWSHORT")
        await _short(db, user_id=user.id, pair_id=short_pair.id, principal="500")
        market, outcomes = await _market(db, title="lmsr_writer_block")
        await db.commit()
        uid, mid, oid = user.id, market.id, outcomes[0].id

    await WRITER.start()
    try:
        with pytest.raises(HTTPException) as exc:
            await WRITER.submit(BuyCmd(mid, oid, uid, "lmsr_writer_short",
                                       D("10"), None, None, True))
        assert exc.value.status_code == 400
        assert "insufficient_initial_margin" in str(exc.value.detail)
    finally:
        await WRITER.stop()

    async with async_session_maker() as db:
        assert (await db.get(User, uid)).cash == D("100")
        assert (await db.get(Outcome, oid)).total_shares == ZERO
        assert (await db.execute(select(Position))).scalars().all() == []
        assert (await db.execute(select(FxTrade))).scalars().all() == []
    assert not GATES.held_keys()


async def test_foreign_only_short_blocks_legacy_lmsr_buy():
    from app.api.v1.market import buy_shares
    from app.schemas.market import TradeRequest

    async with async_session_maker() as db:
        user = await _user(db, name="lmsr_legacy_short")
        short_pair = await _pair(db, code="LMSRLSHORT")
        await _short(db, user_id=user.id, pair_id=short_pair.id, principal="500")
        market, outcomes = await _market(db, title="lmsr_legacy_block")
        await db.commit()
        uid, oid = user.id, outcomes[0].id

    async with async_session_maker() as db:
        user = await db.get(User, uid)
        with pytest.raises(HTTPException) as exc:
            await buy_shares(
                TradeRequest(outcome_id=oid, shares=D("10"), accept_any_slippage=True),
                user, db,
            )
        assert exc.value.status_code == 400
        assert "insufficient_initial_margin" in str(exc.value.detail)

        assert (await db.get(User, uid)).cash == D("100")
        assert (await db.get(Outcome, oid)).total_shares == ZERO
        assert (await db.execute(select(Position))).scalars().all() == []
    assert not GATES.held_keys()


# ─────────────────────────── loan quota / borrow ───────────────────────────

async def test_quota_known_short_uses_shared_headroom_not_gold_only():
    async with async_session_maker() as db:
        user = await _user(db, name="quota_known", cash="100")
        pair = await _pair(db, code="QUOTAKNOWN")
        await _short(db, user_id=user.id, pair_id=pair.id, principal="10")
        await _config(db, loan_enabled="true", loan_daily_rate="0")
        await db.commit()
        site_config.clear_cache()
        uid = user.id

    k = _k(pair.id, "10")
    async with async_session_maker() as db:
        quota = await get_quota(user=await db.get(User, uid), db=db)

    assert quota.net_worth is not None and quota.net_worth > ZERO
    assert quota.max_borrow > ZERO
    assert quota.max_borrow == TH4.max_new_gold_loan(
        equity=quota.net_worth, debt=quota.debt,
        positive_assets=ZERO, short_cover=k,
    )
    # The gold-only formula ignores the alpha*K shared-risk reservation.
    assert quota.max_borrow < TH4.max_borrow(quota.net_worth, quota.debt)
    assert quota.blocked_reason is None


async def test_quota_unknown_short_returns_null_equity_and_zero_borrow():
    async with async_session_maker() as db:
        user = await _user(db, name="quota_unknown", cash="100")
        thin = await _pair(db, code="QUOTAUNKNOWN", gold="100", foreign="10")
        # q == F: the pool cannot quote the whole obligation -> K unknown.
        await _short(db, user_id=user.id, pair_id=thin.id, principal="10")
        await _config(db, loan_enabled="true", loan_daily_rate="0")
        await db.commit()
        site_config.clear_cache()
        uid = user.id

    async with async_session_maker() as db:
        quota = await get_quota(user=await db.get(User, uid), db=db)

    assert quota.net_worth is None
    assert quota.liquidation_equity is None
    assert quota.max_borrow == ZERO
    assert quota.blocked_reason is not None
    assert "insufficient_pool_foreign" in quota.blocked_reason


async def test_foreign_only_short_blocks_gold_borrow_and_keeps_balances():
    async with async_session_maker() as db:
        user = await _user(db, name="borrow_foreign_only", cash="10")
        pair = await _pair(db, code="BORROWFOSHORT")
        await _short(db, user_id=user.id, pair_id=pair.id, principal="500")
        await _config(db, loan_enabled="true", loan_daily_rate="0")
        await db.commit()
        site_config.clear_cache()
        uid = user.id

    async with async_session_maker() as db:
        with pytest.raises(HTTPException) as exc:
            await borrow(BorrowRequest(amount="1"), user=await db.get(User, uid), db=db)
        assert exc.value.status_code == 400
        assert "insufficient_initial_margin" in str(exc.value.detail)

    async with async_session_maker() as db:
        user = await db.get(User, uid)
        assert user.cash == D("10") and user.debt == ZERO
        short = (await db.execute(select(FxShortPosition).where(
            FxShortPosition.user_id == uid))).scalars().one()
        assert short.principal_foreign == D("500")
    assert not GATES.held_keys()


async def test_unknown_short_cover_cost_borrow_is_rejected_not_500():
    async with async_session_maker() as db:
        user = await _user(db, name="borrow_unknown", cash="100")
        thin = await _pair(db, code="BORROWUNKNOWN", gold="100", foreign="10")
        await _short(db, user_id=user.id, pair_id=thin.id, principal="10")
        await _config(db, loan_enabled="true", loan_daily_rate="0")
        await db.commit()
        site_config.clear_cache()
        uid = user.id

    async with async_session_maker() as db:
        with pytest.raises(HTTPException) as exc:
            await borrow(BorrowRequest(amount="1"), user=await db.get(User, uid), db=db)
        assert exc.value.status_code == 400
        assert "insufficient_pool_foreign" in str(exc.value.detail)

    async with async_session_maker() as db:
        assert (await db.get(User, uid)).debt == ZERO
    assert not GATES.held_keys()


class _RecordingGates:
    """Record hold arguments, then delegate to the real gate registry."""

    def __init__(self, inner):
        self.inner = inner
        self.holds: list[tuple[tuple, tuple]] = []

    def hold(self, *, exclusive=(), shared=()):
        self.holds.append((tuple(exclusive), tuple(shared)))
        return self.inner.hold(exclusive=exclusive, shared=shared)


async def test_borrow_foreign_only_account_holds_short_pair_gate(monkeypatch):
    from app.api.v1 import loan as loan_api
    from app.services.credit.keys import GroupKey

    async with async_session_maker() as db:
        user = await _user(db, name="borrow_gates", cash="100000")
        pair = await _pair(db, code="BORROWGATE")
        await _short(db, user_id=user.id, pair_id=pair.id, principal="10")
        await _config(db, loan_enabled="true", loan_daily_rate="0")
        await db.commit()
        site_config.clear_cache()
        uid, pid = user.id, pair.id

    proxy = _RecordingGates(GATES)
    monkeypatch.setattr(loan_api, "GATES", proxy)
    async with async_session_maker() as db:
        result = await borrow(BorrowRequest(amount="1"), user=await db.get(User, uid), db=db)
    assert result.debt == D("1")
    key = GroupKey("fx", pid)
    assert any(key in shared for _, shared in proxy.holds)
    assert not GATES.held_keys()
