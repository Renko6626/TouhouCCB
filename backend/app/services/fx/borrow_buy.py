"""Atomic financed FX buys and their read-only advisory quotes.

The spot executor owns product math/rows; this wrapper owns one transaction.
Quotes and writes share credit.check_new_risk's full valuation projection.
"""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import User
from app.models.fx import FxWallet
from app.schemas.fx import FxBorrowBuyQuote, FxBorrowBuyResponse
from app.services import loan_service, site_config
from app.services.credit import flags
from app.services.credit.account_read import classify_account_risk
from app.services.credit.cash import available_cash
from app.services.credit.gates import GATES
from app.services.credit.keys import GroupKey
from app.services.credit.risk import PostTradeState, check_new_risk, discover_dependencies
from app.services.credit.version import economic_version_of
from app.services.fx import publisher, trading

MAX_GOLD = Decimal('9999999999.999999')


def validate_funding(amount, borrow_amount):
    amount = trading._positive(amount, 'amount')
    borrowed = trading._positive(borrow_amount, 'borrow_amount')
    if borrowed > amount:
        raise trading.TradeRejected('borrow_amount cannot exceed amount')
    if amount > MAX_GOLD:
        raise trading.TradeRejected('amount exceeds storage limit')
    return amount, borrowed


def check_storage_bounds(user, quote, borrowed, debt_after, *, foreign_amount, cost_basis):
    values = (Decimal(user.cash) + borrowed, debt_after, quote.output_amount,
              quote.post_gold_reserve, quote.post_foreign_reserve,
              Decimal(foreign_amount) + quote.output_amount,
              Decimal(cost_basis) + quote.input_amount)
    if any(not value.is_finite() or value < 0 or value > MAX_GOLD for value in values):
        raise trading.TradeRejected('financed trade exceeds storage limit')


def response(execution):
    t = execution.trade
    return FxBorrowBuyResponse(trade_id=t.id, pair_id=t.pair_id,
        input_amount=t.input_amount, borrow_amount=t.borrow_amount,
        cash_amount=t.input_amount - t.borrow_amount, output_amount=t.output_amount,
        fee_amount=t.fee_amount, post_price=t.post_price,
        replay=execution.replay, created_at=t.created_at)


async def execute_borrow_buy(db: AsyncSession, user_id: int, pair_id: int,
                             amount: Decimal, borrow_amount: Decimal,
                             min_out: Decimal, idempotency_key: str) -> FxBorrowBuyResponse:
    amount, borrow_amount = validate_funding(amount, borrow_amount)
    trading._require_writes()
    key = GroupKey('fx', pair_id)
    for attempt in range(flags.get_flags().credit_risk_retry_limit + 1):
        try:
            deps = await discover_dependencies(db, user_id, extra_groups=[key])
            await db.rollback()  # Never hold a connection while waiting for gates.
            async with GATES.hold(exclusive=[key], shared=deps.groups):
                try:
                    execution = await trading.execute_trade_in_session(
                        db, user_id, pair_id, 'buy', amount, min_out, idempotency_key,
                        credit_deps=deps, borrow_amount=borrow_amount)
                    result = response(execution)
                    if execution.replay:
                        await db.rollback()
                        return result
                    trading._require_writes()
                    await db.commit()
                    trading.notify_market_data_committed(pair_id)
                except BaseException:
                    await trading._rollback_quietly(db)
                    raise
            # Already materialized: no post-commit ORM refresh or risk reads.
            publisher.enqueue_publication(pair_id=pair_id, post_price=result.post_price,
                                          trade_id=result.trade_id)
            return result
        except trading._RetryCredit:
            if attempt == flags.get_flags().credit_risk_retry_limit:
                raise HTTPException(status_code=409, detail='version_conflict; retry') from None
        except BaseException:
            await trading._rollback_quietly(db)
            raise


async def quote_borrow_buy(db: AsyncSession, user_id: int, pair_id: int,
                           amount: Decimal, borrow_amount: Decimal) -> FxBorrowBuyQuote:
    amount, borrowed = validate_funding(amount, borrow_amount)
    now = trading.utcnow()
    result = FxBorrowBuyQuote(pair_id=pair_id, input_amount=amount, borrow_amount=borrowed,
        cash_amount=amount - borrowed, expires_at=now + timedelta(seconds=30))
    pair = await trading._pair(db, pair_id)

    def blocked(reason):
        result.blocked_reason = reason
        return result

    thresholds = flags.get_flags().thresholds
    if thresholds is None:
        return blocked('risk_engine_unavailable')
    result.leverage, result.r_initial = thresholds.leverage, thresholds.r_initial
    result.r_maintenance = thresholds.r_maintenance
    if not await site_config.get_bool_or(db, 'loan_enabled', False):
        return blocked('loan_disabled')
    if not await site_config.get_bool_or(db, 'fx_enabled', False):
        return blocked('FX trading is disabled')
    if not trading._player_side_allowed(pair.status, bool(pair.reduce_only), 'buy'):
        return blocked('FX pair is reduce-only' if pair.reduce_only else 'FX pair is not trading')
    deps = await discover_dependencies(db, user_id, extra_groups=[GroupKey('fx', pair_id)])
    result.daily_rate = deps.daily_rate
    user = (await db.execute(select(User).where(User.id == user_id)
                .execution_options(populate_existing=True))).scalar_one()
    if economic_version_of(user) != deps.economic_version:
        return blocked('version_conflict')
    if user.is_bot:
        return blocked('bot accounts cannot trade FX')
    if user.tos_accepted_at is None:
        return blocked('TOS acceptance required')
    key = GroupKey('fx', pair_id)
    snapshot = deps.snapshots.get(key)
    if snapshot and snapshot.short_debt is not None:
        return blocked('same_pair_short')
    result.available_cash = await available_cash(db, user)
    result.affordable = result.available_cash >= amount - borrowed
    if not result.affordable:
        return blocked('insufficient cash')
    # The quote uses no FOR UPDATE, write gate or financial mutation.
    pair = await trading._pair(db, pair_id)
    if snapshot is None or snapshot.pool_version != pair.pool_version:
        return blocked('version_conflict')
    q = trading._math(pair, 'buy', amount)
    result.output_amount, result.fee_amount = q.output_amount, q.fee_amount
    result.effective_price = q.effective_price
    result.post_price = q.post_price
    debt_after = loan_service.pending_debt(user, deps.daily_rate, now) + borrowed
    holdings = deps.holdings.get(key, {}).get(pair_id, Decimal('0')) + q.output_amount
    # Numeric bounds also cover the intermediate loan credit before its cash is spent.
    basis = (await db.execute(select(FxWallet.cost_basis).where(
        FxWallet.user_id == user_id, FxWallet.pair_id == pair_id))).scalar_one_or_none() or Decimal('0')
    check_storage_bounds(user, q, borrowed, debt_after,
                         foreign_amount=holdings - q.output_amount, cost_basis=basis)
    decision = await check_new_risk(db, user=user,
        deps=replace(deps, debt_last_accrued_at=now),
        post=PostTradeState(cash=Decimal(user.cash) + borrowed - amount, debt=debt_after,
            fx_reserves={pair_id: (q.post_gold_reserve, q.post_foreign_reserve)},
            post_holdings={key: {pair_id: holdings}}, base_versions={key: snapshot.version}),
        thresholds=thresholds, partial_pct=Decimal('1'), now=now, include_valuation=True)
    result.estimated_debt = decision.debt_after
    if decision.holdings_value is not None:
        result.estimated_equity, result.estimated_risk_basis = decision.equity_after, decision.risk_basis
        result.margin_status = classify_account_risk(thresholds, equity=decision.equity_after,
            debt=decision.debt_after, positive_assets=decision.holdings_value,
            short_cover=decision.short_cover_cost, blocked_reason=None)
        if decision.risk_basis and decision.equity_after is not None:
            result.equity_to_risk_basis = decision.equity_after / decision.risk_basis
    result.executable = decision.allowed
    result.blocked_reason = decision.reason
    return result
