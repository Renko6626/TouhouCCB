"""Short cash proceeds must not create rank wealth or hide unpriced debt."""
from decimal import Decimal
from dataclasses import replace
from app.services.credit import valuation as valuation_mod

import pytest

from app.api.v1.admin_stats import wealth_stats
from app.api.v1.market import _leaderboard_uncached
from app.api.v1.user import get_user_summary
from app.core.database import async_session_maker
from app.services.fx.valuation import compute_total_net_worth
from app.services.credit.valuation import value_user_detailed
from tests.test_credit_short_valuation import _user, _pair, _short, _enable_unified, clean_flags

pytestmark = pytest.mark.asyncio


async def test_short_proceeds_display_matches_account_and_does_not_jump_rank():
    _enable_unified()
    async with async_session_maker() as s:
        user = await _user(s, cash="100")
        peer = await _user(s, cash="105")
        before = (await compute_total_net_worth(s, [user.id]))[user.id]
        pair = await _pair(s)
        # Seed a sale: cash contains the locked proceeds, and foreign debt remains.
        user.cash += Decimal("10")
        await _short(s, user, pair, principal="10", restricted="10")
        after = (await compute_total_net_worth(s, [user.id]))[user.id]
        account = await value_user_detailed(s, user.id, daily_rate=Decimal("0"))
        summary = await get_user_summary(user=user, db=s)
        assert after == account.display_equity == summary["display_equity"] == before
        board = await _leaderboard_uncached(10, "net_worth", s)
        assert [item.user_id for item in board] == [peer.id, user.id]


async def test_pair_prices_adjust_separate_foreign_liabilities():
    _enable_unified()
    async with async_session_maker() as s:
        user = await _user(s, cash="100")
        p1 = await _pair(s)
        p2 = await _pair(s, gold="200")
        await _short(s, user, p1, principal="10", restricted="10")
        await _short(s, user, p2, principal="5", restricted="10")
        assert (await compute_total_net_worth(s, [user.id]))[user.id] == Decimal("80")
        p2.gold_reserve = Decimal("400")
        await s.commit()
        assert (await compute_total_net_worth(s, [user.id]))[user.id] == Decimal("70")
        stats = await wealth_stats(admin=user, db=s)
        assert stats["total_short_marginal_debt"] == 30
        assert stats["total_net_worth"] == (
            stats["total_cash"] + stats["total_holdings_value"]
            - stats["total_debt"] - stats["total_short_marginal_debt"]
        )


async def test_unknown_marginal_debt_excluded_from_rank_and_distribution(monkeypatch):
    _enable_unified()
    async with async_session_maker() as s:
        user = await _user(s, cash="1000")
        known = await _user(s, cash="100")
        pair = await _pair(s)
        real_quote = valuation_mod.quote_fx_short_group
        def invalid_quote(snapshot, *, foreign_debt):
            return real_quote(replace(snapshot, gold_reserve=Decimal("0")),
                              foreign_debt=foreign_debt)
        monkeypatch.setattr(valuation_mod, "quote_fx_short_group", invalid_quote)
        await _short(s, user, pair, principal="10", restricted="10")
        assert (await compute_total_net_worth(s, [user.id]))[user.id] is None
        board = await _leaderboard_uncached(10, "net_worth", s)
        assert [item.user_id for item in board] == [known.id]
        stats = await wealth_stats(admin=known, db=s)
        assert stats["user_count"] == 1
        assert stats["unknown_user_count"] == 1
        assert stats["mean"] == 100
        assert stats["total_cash"] == 100
        assert stats["total_debt"] == 0
        assert stats["total_holdings_value"] == 0
