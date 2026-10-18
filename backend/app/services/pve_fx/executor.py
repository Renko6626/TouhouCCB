"""Controlled real spot execution. No borrowing or separate monetary ledger."""
import json
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.base import User
from app.models.bot import BotProfile
from app.models.fx import FxTreasury, FxWallet
from app.services.credit.gates import GATES
from app.services.credit.keys import GroupKey
from app.services.fx import trading
from app.services.fx.amm import amount_down
from app.services.market_locks import lock_user
from app.services.pve_fx.registry import get_strategy
from app.services.pve_fx.types import FxDecision, FxExecutionResult, FxFill, FxOrderIntent, FxTickFrame


async def _profile(db, profile_id):
    row = (await db.execute(select(BotProfile).where(BotProfile.id == profile_id,
                            BotProfile.market_kind == 'fx'))).scalars().first()
    if row is None:
        raise trading.TradeRejected('FX bot not found')
    return row


def _same_episode(left, right):
    if not left or not right:
        return not left and not right
    return all(left.get(k) == right.get(k) for k in ('direction', 'entry_tick', 'remaining', 'basis'))


async def save_observation_state(profile_id: int, decision: FxDecision, frame: FxTickFrame) -> dict:
    """Persist only peak/reason, including waiting decisions, in its own transaction.

    Engine calls this for waiting decisions only; execute_bot_order calls it for
    orders. Never copies pending intents or fill-dependent quantity/cost. A stale
    decision cannot restore a completed episode or overwrite a pause.
    """
    async with async_session_maker() as db:
        profile = await _profile(db, profile_id)
        uid, pid = profile.user_id, profile.fx_pair_id
        await db.rollback()
        async with GATES.hold(exclusive=[GroupKey('fx', pid)]):
            await trading._pair(db, pid, lock=True)
            await lock_user(db, uid)
            await trading._wallet_lock(db, uid, pid, create=False)
            await db.execute(select(FxTreasury).where(FxTreasury.pair_id == pid).with_for_update())
            profile = (await db.execute(select(BotProfile).where(BotProfile.id == profile_id)
                       .with_for_update().execution_options(populate_existing=True))).scalars().one()
            state = deepcopy(profile.strategy_state)
            observed = decision.state.get('episode')
            current = state.get('episode')
            if (pid == frame.pair_id and profile.status == 'active' and current and observed
                    and _same_episode(current, observed)):
                peak = observed.get('peak_return')
                if peak is not None:
                    current['peak_return'] = max(peak, current['peak_return']) if current.get('peak_return') is not None else peak
                if current.get('closing_reason') is None and observed.get('closing_reason') is not None:
                    current['closing_reason'] = observed['closing_reason']
                profile.strategy_state = state
            trading._require_writes()
            await db.commit()
            return state


def _fill(execution, action, requested, user, wallet):
    t = execution.trade
    cash_after = Decimal(user.cash)
    foreign_after = Decimal(wallet.foreign_amount)
    buy = t.side == 'buy'
    return FxFill(t.id, action, 'gold' if buy else 'foreign', 'foreign' if buy else 'gold',
                  t.input_amount, t.output_amount, t.fee_amount,
                  cash_after + t.input_amount if buy else cash_after - t.output_amount,
                  cash_after, foreign_after - t.output_amount if buy else foreign_after + t.input_amount,
                  foreign_after, t.created_at, requested)


async def _apply_fill_in_session(session, execution, command):
    """Writer-only built-in fill hook; money and profile stay in one transaction."""
    profile = await _profile(session, command.bot_profile_id)
    if execution.replay:
        return FxExecutionResult(state=deepcopy(profile.strategy_state))
    callback_state = json.loads(command.fill_state_json)
    if not _same_episode(profile.strategy_state.get('episode'), callback_state.get('episode')):
        raise trading.TradeRejected('episode changed since decision')
    if profile.template != command.bot_template:
        raise trading.TradeRejected('strategy changed since decision')
    user = await session.get(User, command.user_id)
    wallet = (await session.execute(select(FxWallet).where(
        FxWallet.user_id == command.user_id, FxWallet.pair_id == command.pair_id))).scalars().one()
    fill = _fill(execution, command.bot_action, command.requested_amount, user, wallet)
    if command.bot_purpose == 'admin_liquidate' and not callback_state.get('episode'):
        state = {} if wallet.foreign_amount == 0 else deepcopy(profile.strategy_state)
    else:
        state = get_strategy(command.bot_template).on_fill(callback_state, fill)
    profile.strategy_state = state
    profile.last_trade_at = fill.executed_at
    await session.flush()
    return FxExecutionResult(fill=fill, state=state)


async def _execute(profile_id, decision, frame, *, purpose='scheduled', operator_id=None, min_out=Decimal('0')):
    state = {}
    try:
        state = (await save_observation_state(profile_id, decision, frame)
                 if purpose == 'scheduled' else {})
        if decision.intent is None:
            return FxExecutionResult(state=state)
        intent = decision.intent
        async with async_session_maker() as db:
            profile = await _profile(db, profile_id)
            uid, pid, template = profile.user_id, profile.fx_pair_id, profile.template
            if pid != frame.pair_id:
                raise trading.TradeRejected('FX bot pair mismatch')
            state = deepcopy(profile.strategy_state)
            if not _same_episode(state.get('episode'), decision.state.get('episode')):
                raise trading.TradeRejected('episode changed since decision')
            callback_state = deepcopy(decision.state)
            if state.get('episode'):
                callback_state['episode'] = deepcopy(state['episode'])
            amount = intent.amount
            if callback_state.get('episode') and not callback_state.get('pending', {}).get('opening', True):
                amount = min(amount, Decimal(callback_state['episode']['remaining']))
            if amount.as_tuple().exponent < -6:
                amount = amount_down(amount)
            if amount <= 0:
                raise trading.TradeRejected('amount rounds to zero')
            side = 'sell' if intent.action == 'sell_foreign' else 'buy'
            mode = 'foreign_out' if intent.action == 'buy_foreign' else 'input'
            if purpose == 'scheduled':
                event_key = uuid4().hex if template == 'random' else str(frame.tick_id)
                key = f'fxbot:{profile_id}:{event_key}'
            else:
                key = f'fxbot-admin:{profile_id}:{uuid4().hex}'

            from app.services.fx.writer import FX_WRITER
            from app.services.fx.writer_ops import BotTrade
            command = BotTrade(pair_id=int(pid), user_id=int(uid), side=side,
                amount=amount, min_out=min_out, idempotency_key=key,
                amount_mode=mode, bot_profile_id=int(profile_id), bot_purpose=purpose,
                bot_operator_id=operator_id, bot_reference_price=frame.reference_price,
                fill_state_json=json.dumps(callback_state), bot_template=template,
                bot_action=intent.action, requested_amount=intent.amount)
            await db.rollback()
        return await FX_WRITER.submit(command)

    except (HTTPException, trading.TradeRejected, ValueError, ArithmeticError) as exc:
        return FxExecutionResult(state=state, rejection=str(exc.detail if isinstance(exc, HTTPException) else exc))


async def execute_bot_order(profile_id: int, decision: FxDecision, frame: FxTickFrame) -> FxExecutionResult:
    return await _execute(profile_id, decision, frame)


async def liquidate_fx_bot(profile_id: int, min_out: Decimal, operator_id: int) -> FxExecutionResult:
    """One ordinary spot batch, paused superuser operation; repeat partial exits manually."""
    from datetime import datetime, timezone
    async with async_session_maker() as db:
        profile = await _profile(db, profile_id)
        state, pid = deepcopy(profile.strategy_state), profile.fx_pair_id
        episode = state.get('episode')
        if episode:
            action = 'sell_foreign' if episode['direction'] == 1 else 'buy_foreign'
            amount = Decimal(episode['remaining'])
            state['pending'] = {'tick': -1, 'opening': False}
        else:
            wallet = (await db.execute(select(FxWallet).where(FxWallet.user_id == profile.user_id,
                                      FxWallet.pair_id == pid))).scalars().first()
            amount = Decimal(wallet.foreign_amount) if wallet else Decimal('0')
            action = 'sell_foreign'
        if amount <= 0:
            return FxExecutionResult(state=state, rejection='no position to liquidate')
    frame = FxTickFrame(-1, pid, datetime.now(timezone.utc), (), Decimal('0'))
    return await _execute(profile_id, FxDecision(FxOrderIntent(action, amount, 'admin_liquidate'), state),
                          frame, purpose='admin_liquidate', operator_id=operator_id, min_out=min_out)
