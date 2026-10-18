"""Typed FX commands and writer-owned transaction boundaries.

Commands retain only immutable request values. Discovery is repeated in a fresh
session on credit conflicts; Pair/User locks and the existing financial kernels
remain responsible for validating execution state. The transitional GATE stays
held through commit and synchronous installation.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.core import database
from app.schemas.fx import FxNetTradeRequest, FxTradePublic
from app.services.account_events import capture_accounts, publish_accounts
from app.services.credit import flags
from app.services.credit.gates import GATES
from app.services.credit.keys import GroupKey
from app.services.credit.risk import discover_dependencies
from app.services.fx import borrow_buy, net_trading, publisher, shorts, trading
from app.services.market_read_state import capture_fx_changes, invalidate_fx_changes


@dataclass(frozen=True, slots=True)
class SpotTrade:
    pair_id: int
    user_id: int
    side: str
    amount: Decimal
    min_out: Decimal
    idempotency_key: str
    amount_mode: str = 'input'
    bot_profile_id: int | None = None
    bot_purpose: str = 'scheduled'
    bot_operator_id: int | None = None
    bot_reference_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class BotTrade(SpotTrade):
    """Built-in PvE fill input, serialized before leaving the producer session."""
    fill_state_json: str = '{}'
    bot_template: str = ''
    bot_action: str = ''
    requested_amount: Decimal = Decimal('0')


@dataclass(frozen=True, slots=True)
class BorrowBuy:
    pair_id: int
    user_id: int
    amount: Decimal
    borrow_amount: Decimal
    min_out: Decimal
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class NetTrade:
    pair_id: int
    user_id: int
    side: str
    amount: Decimal
    borrow_amount: Decimal
    allow_growth: bool
    min_out: Decimal
    idempotency_key: str
    expected_economic_version: int


@dataclass(frozen=True, slots=True)
class ShortOpen:
    pair_id: int
    user_id: int
    foreign_amount: Decimal
    min_gold_out: Decimal
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ShortCover:
    pair_id: int
    user_id: int
    foreign_amount: Decimal | None
    cover_all: bool
    max_gold_in: Decimal
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    response: object
    replay: bool
    post_price: Decimal
    trade_id: int
    reason: str


async def _execute_in_session(db, cmd, deps):
    if isinstance(cmd, SpotTrade):
        execution = await trading.execute_trade_in_session(
            db, cmd.user_id, cmd.pair_id, cmd.side, cmd.amount, cmd.min_out,
            cmd.idempotency_key, credit_deps=deps, amount_mode=cmd.amount_mode,
            bot_profile_id=cmd.bot_profile_id, bot_purpose=cmd.bot_purpose,
            bot_operator_id=cmd.bot_operator_id, bot_reference_price=cmd.bot_reference_price)
        if not execution.replay:
            # Match the persisted Numeric precision returned by the original
            # spot wrapper, and materialize before the writer commits/closes.
            await db.refresh(execution.trade)
            execution = replace(execution, public=FxTradePublic.model_validate(execution.trade))
        response = execution.public
        if isinstance(cmd, BotTrade):
            from app.services.pve_fx.executor import _apply_fill_in_session
            response = await _apply_fill_in_session(db, execution, cmd)
        return ExecutionResult(response, execution.replay, execution.public.post_price,
                               execution.public.id, 'trading')
    if isinstance(cmd, BorrowBuy):
        execution = await trading.execute_trade_in_session(
            db, cmd.user_id, cmd.pair_id, 'buy', cmd.amount, cmd.min_out,
            cmd.idempotency_key, credit_deps=deps, borrow_amount=cmd.borrow_amount)
        response = borrow_buy.response(execution)
        return ExecutionResult(response, execution.replay, response.post_price,
                               response.trade_id, 'borrow_buy')
    if isinstance(cmd, NetTrade):
        req = FxNetTradeRequest(side=cmd.side, amount=cmd.amount,
            borrow_amount=cmd.borrow_amount, allow_growth=cmd.allow_growth,
            min_out=cmd.min_out, idempotency_key=cmd.idempotency_key,
            expected_economic_version=cmd.expected_economic_version)
        response = await net_trading.execute_net_trade_in_session(
            db, user_id=cmd.user_id, pair_id=cmd.pair_id, req=req, credit_deps=deps)
        return ExecutionResult(response, response.replay, response.post_price,
                               response.trade_ids[-1], 'net_trading')
    if isinstance(cmd, ShortOpen):
        execution = await shorts.execute_short_open_in_session(
            db, user_id=cmd.user_id, pair_id=cmd.pair_id,
            foreign_amount=cmd.foreign_amount, min_gold_out=cmd.min_gold_out,
            idempotency_key=cmd.idempotency_key, credit_deps=deps)
    elif isinstance(cmd, ShortCover):
        execution = await shorts.execute_short_cover_in_session(
            db, user_id=cmd.user_id, pair_id=cmd.pair_id,
            foreign_amount=cmd.foreign_amount, cover_all=cmd.cover_all,
            max_gold_in=cmd.max_gold_in, idempotency_key=cmd.idempotency_key,
            credit_deps=deps)
    else:
        raise TypeError(f'Unsupported FX command: {type(cmd).__name__}')
    return ExecutionResult(shorts._short_response(execution), execution.replay,
                           execution.post_price, execution.trade_id, 'shorts')


async def execute_command(writer, cmd):
    """Commit, install, then emit hints before allowing the next pair command."""
    trading._require_writes()
    target = GroupKey('fx', cmd.pair_id)
    needs_discovery = isinstance(cmd, (BorrowBuy, ShortOpen, ShortCover)) or (
        isinstance(cmd, NetTrade) and cmd.borrow_amount > 0)
    retry_limit = max(0, int(flags.get_flags().credit_risk_retry_limit))
    # Spot/net optimistically start with only the target. This extra attempt
    # preserves their pre-writer credit retry budget.
    attempts = retry_limit + (1 if needs_discovery else 2)
    for attempt in range(attempts):
        async with database.async_session_maker() as db:
            try:
                deps = (await discover_dependencies(db, cmd.user_id, extra_groups=[target])
                        if needs_discovery else None)
                if db.in_transaction():
                    await db.rollback()
                async with GATES.hold(exclusive=[target], shared=deps.groups if deps else ()):
                    execution = await _execute_in_session(db, cmd, deps)
                    if execution.replay:
                        await db.rollback()
                        return execution.response
                    trading._require_writes()
                    await db.flush()
                    changes = capture_fx_changes(db)
                    account_hints = capture_accounts(db)
                    try:
                        await db.commit()
                    except BaseException:
                        invalidate_fx_changes(changes)
                        raise
                    for snapshot in changes:
                        writer.install_committed(snapshot)
                    publish_accounts(account_hints, execution.reason)
                    trading.notify_market_data_committed(cmd.pair_id)
                    publisher.enqueue_publication(pair_id=cmd.pair_id,
                        post_price=execution.post_price, trade_id=execution.trade_id)
                    return execution.response
            except (trading._RetryCredit, shorts.ShortRetryCredit):
                await trading._rollback_quietly(db)
                needs_discovery = True
                if attempt == attempts - 1:
                    raise HTTPException(409, 'version_conflict; retry') from None
            except IntegrityError as exc:
                await trading._rollback_quietly(db)
                if isinstance(cmd, (ShortOpen, ShortCover)) and shorts._is_fx_trade_idempotency_conflict(exc):
                    raise HTTPException(409, 'idempotency key conflict; retry') from None
                raise
            except (ValueError, ArithmeticError) as exc:
                await trading._rollback_quietly(db)
                if isinstance(cmd, NetTrade):
                    raise trading.TradeRejected(str(exc)) from exc
                raise
            except BaseException:
                await trading._rollback_quietly(db)
                raise


def register_all_ops(writer):
    for command_type in (SpotTrade, BotTrade, BorrowBuy, NetTrade, ShortOpen, ShortCover):
        writer.register_op(command_type, execute_command)
