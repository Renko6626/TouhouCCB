"""Behavioral tests for FX display valuation on an isolated SQLite database."""
from datetime import datetime, timezone
from decimal import Decimal

from app.models.base import Market, Outcome, Position, User
from app.models.fx import FxWallet
from app.services.fx.valuation import compute_fx_mtm, compute_total_net_worth
from app.services.fx import trading
from app.api.v1.user import get_user_summary
from app.api.v1.market import _leaderboard_uncached
from fastapi import HTTPException
from tests.fx_test_helpers import fx_db, add_pair  # noqa: F401
from sqlmodel import SQLModel
import pytest
import pytest_asyncio
from app.core.database import engine as app_engine


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _dispose_app_engine():
    yield
    await app_engine.dispose()


async def _add_user(db, name: str, cash: str = "0", debt: str = "0") -> User:
    user = User(username=name, cash=Decimal(cash), debt=Decimal(debt))
    db.add(user)
    await db.flush()
    return user


def _create_core_tables(db):
    """Extend the FX fixture's real SQLite schema for cross-domain reads."""
    SQLModel.metadata.create_all(db._session.bind)


@pytest.mark.asyncio
async def test_fx_mtm_uses_latest_pair_marginal_price_and_six_decimals(fx_db):
    pair, _ = await add_pair(fx_db, gold="120", foreign="80")
    user = await _add_user(fx_db, "mtm-user")
    fx_db.add(FxWallet(user_id=user.id, pair_id=pair.id,
                       foreign_amount=Decimal("2.123456"), cost_basis=Decimal("1")))
    await fx_db.commit()

    values = await compute_fx_mtm(fx_db, user_ids=[user.id])
    assert values[user.id] == Decimal("3.185184")


@pytest.mark.asyncio
async def test_total_net_worth_adds_fx_without_changing_debt_or_lmsr(fx_db):
    # The shared FX fixture intentionally creates only FX tables.  Add the
    # ordinary market tables needed by the LMSR MTM query on this isolated DB.
    _create_core_tables(fx_db)
    pair, _ = await add_pair(fx_db, gold="100", foreign="50")
    user = await _add_user(fx_db, "total-user", cash="10", debt="3")
    market = Market(title="lmsr", liquidity_b=100.0, status="trading")
    fx_db.add(market)
    await fx_db.flush()
    out_a = Outcome(market_id=market.id, label="a", total_shares=Decimal("0"))
    out_b = Outcome(market_id=market.id, label="b", total_shares=Decimal("0"))
    fx_db.add_all([out_a, out_b])
    await fx_db.flush()
    fx_db.add(Position(user_id=user.id, outcome_id=out_a.id,
                       amount=Decimal("4"), cost_basis=Decimal("2")))
    fx_db.add(FxWallet(user_id=user.id, pair_id=pair.id,
                       foreign_amount=Decimal("2"), cost_basis=Decimal("1")))
    await fx_db.commit()

    values = await compute_total_net_worth(fx_db, user_ids=[user.id])
    # 10 - 3 cash/debt + 4 * 0.5 LMSR MTM + 2 * 2 FX MTM.
    assert values[user.id] == Decimal("13")
    assert user.debt == Decimal("3")


@pytest.mark.asyncio
async def test_summary_includes_executable_fx_collateral(fx_db):
    _create_core_tables(fx_db)
    pair, _ = await add_pair(fx_db, gold="100", foreign="10")
    user = await _add_user(fx_db, "summary-user", cash="10", debt="10")
    market = Market(title="margin", liquidity_b=100.0, status="trading")
    fx_db.add(market)
    await fx_db.flush()
    out_a = Outcome(market_id=market.id, label="a", total_shares=Decimal("0"))
    out_b = Outcome(market_id=market.id, label="b", total_shares=Decimal("0"))
    fx_db.add_all([out_a, out_b])
    await fx_db.flush()
    fx_db.add(Position(user_id=user.id, outcome_id=out_a.id,
                       amount=Decimal("2"), cost_basis=Decimal("1")))
    fx_db.add(FxWallet(user_id=user.id, pair_id=pair.id,
                       foreign_amount=Decimal("10"), cost_basis=Decimal("2")))
    await fx_db.commit()

    summary = await get_user_summary(user=user, db=fx_db)
    assert summary["fx_mtm"] == Decimal("100")
    assert summary["fx_cost_basis"] == Decimal("2")
    assert summary["fx_unrealized_pnl"] == Decimal("98")
    # FX sells yield50gold here; display value100 remains distinct from executable collateral.
    assert summary["display_equity"] == Decimal("101")
    assert Decimal("50") < summary["liquidation_equity"] < Decimal("51")
    assert summary["risk_status"] == "healthy"


@pytest.mark.asyncio
async def test_leaderboard_orders_and_ranks_with_fx_net_worth(fx_db):
    _create_core_tables(fx_db)
    pair, _ = await add_pair(fx_db, gold="100", foreign="10")
    rich = await _add_user(fx_db, "fx-rich", cash="1")
    plain = await _add_user(fx_db, "cash-rich", cash="50")
    fx_db.add(FxWallet(user_id=rich.id, pair_id=pair.id,
                       foreign_amount=Decimal("40"), cost_basis=Decimal("1")))
    await fx_db.commit()

    rows = await _leaderboard_uncached(2, "net_worth", fx_db)
    assert [row.user_id for row in rows] == [rich.id, plain.id]
    assert rows[0].net_worth == Decimal("401")
    assert rows[0].rank == "人里居民"


@pytest.mark.asyncio
async def test_healthy_debt_user_can_buy_and_sell_repays_debt(fx_db, monkeypatch):
    pair, _ = await add_pair(fx_db, gold="100", foreign="100")
    user = await _add_user(fx_db, "debt-trader", cash="10", debt="1")
    user.tos_accepted_at = datetime.now(timezone.utc)
    fx_db.add(FxWallet(user_id=user.id, pair_id=pair.id,
                       foreign_amount=Decimal("2"), cost_basis=Decimal("2")))
    await fx_db.commit()

    bought = await trading.execute_trade(fx_db, user.id, pair.id, "buy",
                                        Decimal("1"), Decimal("0"), "debt-buy")
    assert bought.input_amount == Decimal("1")
    before = await fx_db.get(User, user.id)
    cash_before, debt_before = before.cash, before.debt

    async def _no_broadcast(_trade):
        return None
    monkeypatch.setattr(trading, "publish_public_event", _no_broadcast)
    sold = await trading.execute_trade(fx_db, user.id, pair.id, "sell",
                                       Decimal("1"), Decimal("0"), "debt-sell")
    assert sold.output_amount > 0
    after = await fx_db.get(User, user.id)
    repaid = min(sold.output_amount, debt_before)
    assert after.debt == debt_before - repaid
    assert after.cash == cash_before + sold.output_amount - repaid
