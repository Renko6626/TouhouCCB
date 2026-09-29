"""WP2：统一组合估值（两套 equity、暂停资产、多 pair、利息不落库、SQL 上界）。"""
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import event, select

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.database import async_session_maker, engine
from app.models.base import Market, Outcome, Position, SiteConfig, User
from app.models.fx import FxPair, FxWallet
from app.services import site_config as site_config_service
from app.services.credit.keys import group_sort_key
from app.services.credit.valuation import value_user_detailed, value_users_batch
from app.services.fx.amm import quote_sell
from app.services.fx.valuation import compute_fx_mtm
from app.services.lmsr import calculate_lmsr_cost, get_current_price, quantize_cost
from app.services.loan_service import pending_debt
from app.services.wealth import (
    compute_users_holdings_value,
    compute_users_holdings_value_mtm,
)

ZERO = Decimal("0")
Q6 = Decimal("0.000001")
RATE = Decimal("0.01")
FUTURE = datetime.now(timezone.utc) + timedelta(days=7)
PAST = datetime.now(timezone.utc) - timedelta(days=1)


async def _user(session, name, *, cash="0", debt="0", accrued=None, version=0):
    user = User(
        username=name, cash=Decimal(cash), debt=Decimal(debt),
        debt_last_accrued_at=accrued, economic_version=version,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _market(session, title, shares, *, status="trading", b=100.0, closes_at=None):
    market = Market(title=title, liquidity_b=b, status=status, closes_at=closes_at)
    session.add(market)
    await session.flush()
    outcomes = [
        Outcome(market_id=market.id, label=f"o{i}", total_shares=Decimal(str(amount)))
        for i, amount in enumerate(shares)
    ]
    session.add_all(outcomes)
    await session.commit()
    for outcome in outcomes:
        await session.refresh(outcome)
    return market, outcomes


async def _position(session, user, outcome, amount):
    session.add(Position(
        user_id=user.id, outcome_id=outcome.id,
        amount=Decimal(str(amount)), cost_basis=ZERO,
    ))
    await session.commit()


async def _pair(session, code, *, gold="100", foreign="100", status="trading",
                reduce_only=False, sell_fee="0", buy_fee="0"):
    pair = FxPair(
        currency_code=code, currency_name=code, status=status,
        reduce_only=reduce_only,
        gold_reserve=Decimal(gold), foreign_reserve=Decimal(foreign),
        sell_fee_rate=Decimal(sell_fee), buy_fee_rate=Decimal(buy_fee),
    )
    session.add(pair)
    await session.commit()
    await session.refresh(pair)
    return pair


async def _wallet(session, user, pair, amount):
    session.add(FxWallet(
        user_id=user.id, pair_id=pair.id,
        foreign_amount=Decimal(str(amount)), cost_basis=ZERO,
    ))
    await session.commit()


def _rolling_group_value(outcomes, positions, *, b, fee_rate=ZERO):
    """测试内独立复算：同一滚动 q 副本、outcome_id 升序。"""
    ordered = sorted(outcomes, key=lambda item: item.id)
    index = {item.id: i for i, item in enumerate(ordered)}
    q = [float(item.total_shares) for item in ordered]
    cost = calculate_lmsr_cost(q, b)
    total = ZERO
    for outcome in ordered:
        amount = positions.get(outcome.id)
        if amount is None or amount <= 0:
            continue
        after = list(q)
        after[index[outcome.id]] -= float(amount)
        new_cost = calculate_lmsr_cost(after, b)
        gross = quantize_cost(cost - new_cost)
        total += gross - quantize_cost(gross * fee_rate)
        q, cost = after, new_cost
    return total


@pytest.mark.asyncio
async def test_two_equities_halt_keeps_mtm_but_zero_liquidation():
    async with async_session_maker() as session:
        user = await _user(session, "wp2-halt", cash="100", debt="50")
        market, outcomes = await _market(session, "trading-m", [100, 100])
        await _position(session, user, outcomes[0], 20)
        halt_market, halt_outcomes = await _market(
            session, "halt-m", [50, 50], status="halt",
        )
        await _position(session, user, halt_outcomes[0], 10)

        valuation = await value_user_detailed(session, user.id, daily_rate=RATE)

        mtm = (Decimal("20") * Decimal(str(get_current_price([100.0, 100.0], 0, 100.0)))
               ).quantize(Q6) + (Decimal("10") * Decimal("0.5")).quantize(Q6)
        assert valuation.mtm_lmsr == mtm
        expected_l = _rolling_group_value(
            outcomes, {outcomes[0].id: Decimal("20")}, b=100.0,
        )
        assert valuation.display_equity == (Decimal("100") + mtm - Decimal("50")).quantize(Q6)
        assert valuation.liquidation_equity == (
            Decimal("100") + expected_l - Decimal("50")
        ).quantize(Q6)
        assert valuation.display_equity != valuation.liquidation_equity

        by_key = {(g.key.product, g.key.group_id): g for g in valuation.groups}
        trading_group = by_key[("lmsr", market.id)]
        halt_group = by_key[("lmsr", halt_market.id)]
        assert trading_group.executable is True and trading_group.value == expected_l
        assert halt_group.executable is False
        assert halt_group.blocked_reason == "market_not_open"
        assert halt_group.value == ZERO
        # HALT 的 MTM 仍在 display，但资金回收为 0
        assert valuation.mtm_lmsr > expected_l


@pytest.mark.asyncio
async def test_paused_fx_zero_l_keeps_mtm_and_reduce_only_executes():
    async with async_session_maker() as session:
        user = await _user(session, "wp2-fx-paused", cash="0", debt="0")
        frozen = await _pair(session, "FROZEN", gold="100", foreign="100",
                             status="paused", reduce_only=False)
        _only = await _pair(session, "ONLY", gold="200", foreign="100",
                            status="paused", reduce_only=True)
        await _wallet(session, user, frozen, 10)
        await _wallet(session, user, _only, 5)

        valuation = await value_user_detailed(session, user.id, daily_rate=RATE)
        expected_only = quote_sell(
            Decimal("5"), Decimal("200"), Decimal("100"), ZERO,
        ).output_amount
        assert valuation.mtm_fx == (Decimal("10") + Decimal("10")).quantize(Q6)
        assert valuation.mtm_fx == (await compute_fx_mtm(session, [user.id]))[user.id]
        assert valuation.display_equity == Decimal("20")
        assert valuation.liquidation_equity == expected_only

        by_key = {(g.key.product, g.key.group_id): g for g in valuation.groups}
        assert by_key[("fx", frozen.id)].blocked_reason == "pair_paused"
        assert by_key[("fx", frozen.id)].executable is False
        assert by_key[("fx", frozen.id)].value == ZERO
        assert by_key[("fx", _only.id)].executable is True
        assert by_key[("fx", _only.id)].value == expected_only


@pytest.mark.asyncio
async def test_multiple_fx_pairs_are_not_merged():
    async with async_session_maker() as session:
        user = await _user(session, "wp2-fx-multi", cash="0", debt="0")
        pair_a = await _pair(session, "AAA", gold="100", foreign="100")
        pair_b = await _pair(session, "BBB", gold="50", foreign="200")
        await _wallet(session, user, pair_a, 10)
        await _wallet(session, user, pair_b, 10)

        valuation = await value_user_detailed(session, user.id, daily_rate=RATE)
        expected_a = quote_sell(Decimal("10"), Decimal("100"), Decimal("100"), ZERO
                                ).output_amount
        expected_b = quote_sell(Decimal("10"), Decimal("50"), Decimal("200"), ZERO
                                ).output_amount
        merged = quote_sell(Decimal("20"), Decimal("150"), Decimal("300"), ZERO
                            ).output_amount

        by_key = {(g.key.product, g.key.group_id): g for g in valuation.groups}
        assert by_key[("fx", pair_a.id)].value == expected_a
        assert by_key[("fx", pair_b.id)].value == expected_b
        assert valuation.liquidation_equity == expected_a + expected_b
        assert valuation.liquidation_equity != merged


@pytest.mark.asyncio
async def test_lmsr_group_uses_rolling_q_and_site_fee_not_independent_sum():
    async with async_session_maker() as session:
        session.add(SiteConfig(key="sell_fee_rate", value="0.01", value_type="decimal"))
        await session.commit()
        site_config_service.clear_cache()

        user = await _user(session, "wp2-rolling", cash="0", debt="0")
        market, outcomes = await _market(session, "rolling-m", [120, 80])
        await _position(session, user, outcomes[0], 30)
        await _position(session, user, outcomes[1], 20)

        valuation = await value_user_detailed(session, user.id, daily_rate=RATE)
        expected = _rolling_group_value(
            outcomes, {outcomes[0].id: Decimal("30"), outcomes[1].id: Decimal("20")},
            b=100.0, fee_rate=Decimal("0.01"),
        )
        legacy = await compute_users_holdings_value(session, [user.id])
        assert valuation.groups[0].value == expected
        assert valuation.liquidation_equity == expected
        assert legacy[user.id] != expected


@pytest.mark.asyncio
async def test_mtm_matches_legacy_display_functions():
    async with async_session_maker() as session:
        user = await _user(session, "wp2-mtm-parity", cash="5", debt="0")
        market, outcomes = await _market(session, "parity-m", [90, 60, 30])
        await _position(session, user, outcomes[1], 12)
        await _position(session, user, outcomes[2], 3)
        pair = await _pair(session, "PAR", gold="120", foreign="80")
        await _wallet(session, user, pair, Decimal("2.5"))

        valuation = await value_user_detailed(session, user.id, daily_rate=RATE)
        lmsr_mtm = (await compute_users_holdings_value_mtm(session, [user.id]))[user.id]
        fx_mtm = (await compute_fx_mtm(session, [user.id]))[user.id]
        assert valuation.mtm_lmsr == lmsr_mtm
        assert valuation.mtm_fx == fx_mtm
        assert valuation.display_equity == (
            Decimal("5") + lmsr_mtm + fx_mtm
        ).quantize(Q6)


@pytest.mark.asyncio
async def test_interest_is_not_persisted_and_matches_pending_debt():
    accrued = datetime.now(timezone.utc) - timedelta(days=1)
    async with async_session_maker() as session:
        user = await _user(session, "wp2-interest", cash="0", debt="1000",
                           accrued=accrued, version=4)
        uid = user.id
        before = (user.debt, user.debt_last_accrued_at)

        valuations = await value_users_batch(session, [user.id], daily_rate=RATE)
        valuation = valuations[user.id]
        assert valuation.debt_persisted == Decimal("1000")
        assert valuation.debt_effective > Decimal("1000")
        assert valuation.debt_effective <= pending_debt(
            user, RATE, datetime.now(timezone.utc),
        )
        assert valuation.liquidation_equity == (
            valuation.cash - valuation.debt_effective
        ).quantize(Q6)
        assert valuation.economic_version == 4

        session.expire_all()
        stored = (await session.execute(
            select(User).where(User.id == uid)
        )).scalars().one()
        assert (stored.debt, stored.debt_last_accrued_at) == before


@pytest.mark.asyncio
async def test_groups_follow_f12_order():
    async with async_session_maker() as session:
        user = await _user(session, "wp2-order", cash="0", debt="0")
        big, big_out = await _market(session, "big-m", [100, 100])
        small, small_out = await _market(session, "small-m", [100, 100])
        halted, halted_out = await _market(session, "halted-m", [100, 100], status="halt")
        await _position(session, user, big_out[0], 40)
        await _position(session, user, small_out[0], 2)
        await _position(session, user, halted_out[0], 30)
        pair = await _pair(session, "ORD", gold="100", foreign="100")
        await _wallet(session, user, pair, 1)

        valuation = await value_user_detailed(session, user.id, daily_rate=RATE)
        assert len(valuation.groups) == 4
        assert list(valuation.groups) == sorted(valuation.groups, key=group_sort_key)


@pytest.mark.asyncio
async def test_buy_fee_rate_does_not_participate_in_fx_liquidation():
    async with async_session_maker() as session:
        user = await _user(session, "wp2-buyfee", cash="0", debt="0")
        pair = await _pair(session, "BUYF", gold="100", foreign="100",
                           sell_fee="0.01", buy_fee="0.5")
        await _wallet(session, user, pair, 10)

        valuation = await value_user_detailed(session, user.id, daily_rate=RATE)
        expected = quote_sell(
            Decimal("10"), Decimal("100"), Decimal("100"), Decimal("0.01"),
        ).output_amount
        assert valuation.groups[0].value == expected


@pytest.mark.asyncio
async def test_sql_count_is_bounded_for_100_users():
    async with async_session_maker() as session:
        market, outcomes = await _market(session, "bulk-m", [100, 100])
        pair = await _pair(session, "BULK", gold="100", foreign="100")
        users = [
            User(username=f"wp2-bulk-{i}", cash=Decimal("10"), debt=Decimal("1"))
            for i in range(100)
        ]
        session.add_all(users)
        await session.commit()
        session.add_all([
            Position(user_id=user.id, outcome_id=outcomes[i % 2].id,
                     amount=Decimal("1"), cost_basis=ZERO)
            for i, user in enumerate(users)
        ])
        session.add_all([
            FxWallet(user_id=user.id, pair_id=pair.id,
                     foreign_amount=Decimal("1"), cost_basis=ZERO)
            for user in users
        ])
        await session.commit()
        site_config_service.clear_cache()

        statements = []

        def _capture(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", _capture)
        try:
            first = await value_users_batch(
                session, [user.id for user in users], daily_rate=RATE,
            )
            first_count = len(statements)
            statements.clear()
            second = await value_users_batch(
                session, [user.id for user in users], daily_rate=RATE,
            )
            second_count = len(statements)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", _capture)

        assert len(first) == len(second) == 100
        assert first_count <= 6, statements
        assert second_count <= 6


@pytest.mark.asyncio
async def test_missing_users_and_lock_path():
    async with async_session_maker() as session:
        user = await _user(session, "wp2-lock", cash="7", debt="0")
        market, outcomes = await _market(session, "lock-m", [100, 100])
        await _position(session, user, outcomes[0], 3)

        assert await value_users_batch(session, [], daily_rate=RATE) == {}
        assert await value_users_batch(session, [999999], daily_rate=RATE) == {}
        with pytest.raises(ValueError):
            await value_user_detailed(session, 999999, daily_rate=RATE)

        locked = await value_user_detailed(session, user.id, daily_rate=RATE, lock=True)
        plain = await value_user_detailed(session, user.id, daily_rate=RATE)
        assert locked == plain
