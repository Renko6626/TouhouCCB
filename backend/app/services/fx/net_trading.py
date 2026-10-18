"""Sequential net FX simulation and one-transaction execution.

AMM math and physical ledgers belong to the existing FX primitives. This
module owns whole-request classification, funding, risk and identity.
"""
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import and_, bindparam, literal, select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import User
from app.models.fx import FxExecution, FxPair, FxShortPosition, FxTrade, FxTreasury, FxWallet
from app.schemas.fx import (FxNetLeg, FxNetQuote, FxNetQuoteRequest,
                            FxNetTradeRequest, FxNetTradeResponse, FxTradeInstruction)
from app.services import loan_service, site_config
from app.services.credit import flags
from app.services.credit.keys import GroupKey
from app.services.credit.risk import DependencySet, PostTradeState, check_new_risk, discover_dependencies
from app.services.credit.version import economic_version_of
from app.services.fx import shorts, trading
from app.services.fx.amm import quote_buy, quote_buy_exact_out, quote_sell
from app.services.fx.quantize import amount_down

ZERO = Decimal('0')
MAX = Decimal('9999999999.999999')


@dataclass
class Simulation:
    quote: FxNetQuote
    maths: list
    cash_after: Decimal
    debt_after: Decimal
    now: object
    daily_rate: Decimal


async def _simulate(db, user, pair, wallet, positions, treasury, req, *, now, deps=None, instruction=None):
    """Read-only projection, shared by advisory quote and locked execution."""
    rate = deps.daily_rate if deps else await site_config.get_decimal_or(db, 'loan_daily_rate', ZERO)
    target = next((p for p in positions if p.pair_id == pair.id), None)
    foreign_debt = shorts.pending_short_debt(target, rate, now) if target else ZERO
    holdings = Decimal(wallet.foreign_amount) if wallet else ZERO
    if holdings > 0 and foreign_debt > 0:
        raise HTTPException(status_code=409, detail='same_pair_long_and_short; manual review required')
    cash = Decimal(user.cash)
    old_debt = loan_service.pending_debt(user, rate, now)
    debt = old_debt
    treasury_gold = Decimal(treasury.gold_balance) if treasury else ZERO
    treasury_foreign = Decimal(treasury.foreign_balance) if treasury else ZERO
    repaid = ZERO
    growth = ZERO
    gold, foreign = Decimal(pair.gold_reserve), Decimal(pair.foreign_reserve)
    maths, legs = [], []

    def add(purpose, side, math):
        nonlocal gold, foreign
        maths.append(math)
        legs.append(FxNetLeg(purpose=purpose, side=side,
            input_currency='gold' if side == 'buy' else pair.currency_code,
            output_currency=pair.currency_code if side == 'buy' else 'gold',
            input_amount=math.input_amount, output_amount=math.output_amount,
            fee_amount=math.fee_amount, pre_gold_reserve=gold, pre_foreign_reserve=foreign,
            post_gold_reserve=math.post_gold_reserve, post_foreign_reserve=math.post_foreign_reserve))
        gold, foreign = math.post_gold_reserve, math.post_foreign_reserve

    borrowed = req.borrow_amount
    reason = None
    def block(value):
        nonlocal reason
        reason = reason or value

    if req.side == 'sell':
        own = min(holdings, req.amount)
        if own:
            q = quote_sell(own, gold, foreign, pair.sell_fee_rate)
            add('spot', 'sell', q)
            treasury_foreign += q.fee_amount
            holdings -= own
            if cash + q.output_amount > MAX:
                block('trade exceeds storage limit')
            repaid = min(debt, q.output_amount) if user.auto_repay else ZERO
            cash += q.output_amount - repaid
            debt -= repaid
        residual = req.amount - own
        if residual:
            q = quote_sell(residual, gold, foreign, pair.sell_fee_rate)
            add('short_open', 'sell', q)
            growth = residual
            foreign_debt += residual
            cash += q.output_amount
            if treasury_foreign < residual:
                block('insufficient treasury foreign balance')
            treasury_foreign += q.fee_amount - residual
    else:
        budget = req.amount
        if foreign_debt:
            try:
                exact = quote_buy_exact_out(foreign_debt, gold, foreign, pair.buy_fee_rate)
            except (ValueError, ArithmeticError):
                exact = None
            q = (exact if exact and budget >= exact.input_amount else
                 quote_buy(budget, gold, foreign, pair.buy_fee_rate))
            if q.output_amount > foreign_debt:
                raise trading.TradeRejected('cover output exceeds foreign debt')
            if q.input_amount > cash:
                block('insufficient cash to cover')
            add('short_cover', 'buy', q)
            treasury_gold += q.fee_amount
            treasury_foreign += q.output_amount
            foreign_debt -= q.output_amount
            cash -= q.input_amount
            budget -= q.input_amount
        if budget:
            if instruction and instruction.auto_borrow:
                available = cash
                borrowed = max(ZERO, budget - available)
                if borrowed > instruction.borrow_limit:
                    block('auto borrow limit exceeded')
            q = quote_buy(budget, gold, foreign, pair.buy_fee_rate)
            add('borrow_buy' if borrowed else 'spot', 'buy', q)
            growth = q.output_amount
            treasury_gold += q.fee_amount
            if cash + borrowed > MAX:
                block('trade exceeds storage limit')
            if borrowed > budget:
                block('borrow_amount exceeds new long budget')
            if cash < budget - borrowed:
                block('insufficient cash')
            cash += borrowed - budget
            debt += borrowed
            holdings += q.output_amount
        elif borrowed:
            block('financing requires a new long leg')

    if not await site_config.get_bool_or(db, 'fx_enabled', False):
        block('FX trading is disabled')
    if any(l.purpose != 'short_cover' for l in legs):
        if user.is_bot:
            block('bot accounts cannot trade FX')
        if user.tos_accepted_at is None:
            block('TOS acceptance required')
    status = str(pair.status)
    if status != 'trading' and not (status == 'paused' and pair.reduce_only):
        block('FX pair is not trading')
    if pair.archived and any(l.purpose == 'short_cover' for l in legs):
        block('FX pair is not coverable')
    if growth:
        if pair.reduce_only or status != 'trading' or pair.archived:
            block('FX pair is reduce-only')
        if any(l.purpose == 'short_open' for l in legs):
            if not await site_config.get_bool_or(db, 'fx_short_enabled', False):
                block('FX short opening is disabled')
            if not await site_config.get_bool_or(db, 'loan_enabled', False):
                block('loans are disabled')
        if borrowed and not await site_config.get_bool_or(db, 'loan_enabled', False):
            block('loan_disabled')
        requires_risk = bool(debt or foreign_debt or any(
            p.pair_id != pair.id and (p.principal_foreign or p.interest_foreign) for p in positions))
        if requires_risk and not reason:
            deps = deps or await discover_dependencies(db, user.id, extra_groups=[GroupKey('fx', pair.id)])
            key = GroupKey('fx', pair.id)
            snapshot = deps.snapshots.get(key)
            decision = await check_new_risk(db, user=user,
                deps=replace(deps, debt_last_accrued_at=now),
                post=PostTradeState(cash=cash, debt=debt,
                    fx_reserves={pair.id: (gold, foreign)}, short_reserves={pair.id: (gold, foreign)},
                    post_holdings={key: {pair.id: holdings}}, short_debt={pair.id: foreign_debt},
                    base_versions={key: snapshot.version} if snapshot else {}),
                thresholds=flags.get_flags().thresholds, partial_pct=Decimal('1'), now=now)
            if not decision.allowed:
                block(decision.reason)
    basis = Decimal(wallet.cost_basis) if wallet else ZERO
    if req.side == 'buy':
        basis += sum((l.input_amount for l in legs if l.purpose in ('spot', 'borrow_buy')), ZERO)
    values = [cash, debt, holdings, basis, gold, foreign, treasury_gold, treasury_foreign,
              *(v for q in maths for v in (q.input_amount, q.output_amount, q.fee_amount, q.post_price))]
    if any(v < 0 or v > MAX or not v.is_finite() for v in values):
        block('trade exceeds storage limit')
    result = FxNetQuote(pair_id=pair.id, side=req.side, input_amount=req.amount,
        output_amount=sum((q.output_amount for q in maths), ZERO),
        fee_amount=sum((q.fee_amount for q in maths), ZERO), post_price=maths[-1].post_price,
        legs=legs, final_direction='long' if holdings > 0 else 'short' if foreign_debt > 0 else 'flat',
        wallet_after=holdings, foreign_debt_after=foreign_debt,
        cash_delta=cash - Decimal(user.cash), gold_debt_delta=debt - Decimal(user.debt),
        growth_amount=growth,
        borrow_amount=borrowed, repaid_gold=repaid, executable=not reason,
        blocked_reason=reason, economic_version=economic_version_of(user),
        pool_version=pair.pool_version, expires_at=now + timedelta(seconds=30))
    return Simulation(result, maths, cash, debt, now, rate)


async def _read_quote_base(db, user_id: int, pair_id: int) -> tuple[FxPair, User, FxWallet | None, FxTreasury | None]:
    row = (await db.execute(
        select(FxPair, User, FxWallet, FxTreasury)
        .select_from(FxPair)
        .outerjoin(User, User.id == user_id)
        .outerjoin(FxWallet, and_(FxWallet.user_id == user_id, FxWallet.pair_id == FxPair.id))
        .outerjoin(FxTreasury, FxTreasury.pair_id == FxPair.id)
        .where(FxPair.id == pair_id)
    )).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="FX pair not found")
    pair, user, wallet, treasury = row
    if user is None:
        raise NoResultFound("No row was found when one was required")
    return pair, user, wallet, treasury


async def quote_net_trade(db, user_id: int, pair_id: int, req: FxNetQuoteRequest) -> FxNetQuote:
    pair, user, wallet, treasury = await _read_quote_base(db, user_id, pair_id)
    positions = list((await db.execute(select(FxShortPosition).where(
        FxShortPosition.user_id == user_id).order_by(FxShortPosition.pair_id))).scalars())
    try:
        return (await _simulate(db, user, pair, wallet, positions, treasury, req, now=trading.utcnow())).quote
    except (ValueError, ArithmeticError) as exc:
        raise trading.TradeRejected(str(exc)) from exc


async def reject_net_key(db, user_id, key):
    """Legacy endpoints share net receipt namespace while holding User lock."""
    from app.models.fx import FxExecution
    if (await db.execute(select(FxExecution.id).where(
            FxExecution.user_id == user_id, FxExecution.idempotency_key == key))).first():
        raise HTTPException(status_code=409, detail='idempotency key parameter mismatch')


# Keep only SQL structure here; request values are bound on every execution.
_NET_IDENTITY_ANCHOR = select(literal(1).label('anchor')).subquery()
_NET_LEGACY_EXISTS = select(FxTrade.id).where(
    FxTrade.user_id == bindparam('net_user_id'),
    FxTrade.idempotency_key == bindparam('net_key')).exists()
_NET_IDENTITY = select(FxExecution, _NET_LEGACY_EXISTS).select_from(
    _NET_IDENTITY_ANCHOR).outerjoin(FxExecution, and_(
        FxExecution.user_id == bindparam('net_user_id'),
        FxExecution.idempotency_key == bindparam('net_key')))


async def _read_net_request_identity(db, user_id: int, key: str) -> tuple[FxExecution | None, bool]:
    """Read receipt and legacy key even when neither has a receipt row."""
    receipt, legacy = (await db.execute(
        _NET_IDENTITY, {'net_user_id': user_id, 'net_key': key})).one()
    return receipt, bool(legacy)


def _canonical_request(pair_id, req):
    # Decimal equality must not depend on caller's trailing zeros.
    payload = req.model_dump(mode='json')
    for key in ('amount', 'borrow_amount', 'min_out'):
        payload[key] = format(getattr(req, key).quantize(Decimal('0.000001')), 'f')
    return {'type': 'net', 'pair_id': pair_id, **payload}


async def execute_net_trade_in_session(db: AsyncSession, *, user_id: int, pair_id: int,
                                       req: FxNetTradeRequest, credit_deps: DependencySet | None = None,
                                       _instruction: FxTradeInstruction | None = None,
                                       _now=None) -> FxNetTradeResponse:
    """Execute in the target writer's transaction; never publish or commit.

    Lock Pair then User, and capture committed risk prices after the User lock.
    """
    from app.schemas.fx import FxNetTradeResponse
    from app.services.market_locks import lock_user
    key = GroupKey('fx', pair_id)
    deps = credit_deps
    payload = (_instruction_payload(pair_id, _instruction) if _instruction
               else _canonical_request(pair_id, req))
    pair = await trading._pair(db, pair_id, lock=True)
    user = await lock_user(db, user_id)
    old, legacy = await _read_net_request_identity(db, user_id, req.idempotency_key)
    if old:
        if old.request_payload != payload:
            raise HTTPException(status_code=409, detail='idempotency key parameter mismatch')
        result = FxNetTradeResponse.model_validate(old.response_payload).model_copy(update={'replay': True})
        return result
    if legacy:
        raise HTTPException(status_code=409, detail='idempotency key parameter mismatch')
    trading._require_writes()
    if economic_version_of(user) != req.expected_economic_version:
        raise HTTPException(status_code=409, detail='economic_version_changed; requote')
    wallet = await trading._wallet_lock(db, user_id, pair_id, create=False)
    positions = await shorts._lock_short_rows(db, user_id)
    requires_deps = bool(req.borrow_amount or (_instruction and _instruction.auto_borrow) or user.debt or any(
        p.principal_foreign or p.interest_foreign for p in positions)
        or (req.side == 'sell' and req.amount > (wallet.foreign_amount if wallet else 0)))
    deps = (await discover_dependencies(db, user_id, extra_groups=[key], account_locked=True)
            if requires_deps else None)
    treasury = await shorts._lock_treasury(db, pair_id)
    now = _now or trading.utcnow()
    simulation = await _simulate(db, user, pair, wallet, positions, treasury, req, now=now, deps=deps, instruction=_instruction)
    quote = simulation.quote
    if quote.growth_amount > 0 and not req.allow_growth:
        raise HTTPException(status_code=409, detail='growth_requires_confirmation; requote')
    if not quote.executable:
        if quote.blocked_reason == 'version_conflict':
            raise trading._RetryCredit()
        raise HTTPException(status_code=400, detail=quote.blocked_reason)
    if quote.output_amount < req.min_out:
        raise HTTPException(status_code=409, detail='quoted output is below min_out')
    if treasury is None:
        treasury = FxTreasury(pair_id=pair_id)
        db.add(treasury)
    # All old interest settles at the simulation's T. New debt
    # is posted only when reaching its new LONG leg.
    if requires_deps:
        await shorts._settle_interest_at_T(db, user=user, positions=positions,
            daily_rate=simulation.daily_rate, now=now, source='fx_net')
    target = next((p for p in positions if p.pair_id == pair_id), None)
    receipt = FxExecution(user_id=user_id, pair_id=pair_id,
        idempotency_key=req.idempotency_key, request_payload=payload,
        response_payload={}, created_at=now)
    db.add(receipt)
    await db.flush()
    trades = []
    for index, (leg, math) in enumerate(zip(quote.legs, simulation.maths), start=1):
        if leg.purpose == 'short_cover':
            effective = Decimal(target.principal_foreign) + Decimal(target.interest_foreign)
            cover = await shorts._write_cover_ledger(db, pair=pair, user=user,
                target=target, positions=positions, treasury=treasury, now=now,
                quote=math, q=math.output_amount, x=math.input_amount,
                q_effective=effective,
                full_cover=math.output_amount == effective, limited_by_cash=False,
                requested_foreign_amount=math.output_amount, cover_all=None,
                max_gold_in=math.input_amount, idempotency_key=None, source='player',
                treasury_before={'gold_balance': treasury.gold_balance,
                    'foreign_balance': treasury.foreign_balance, 'updated_at': treasury.updated_at},
                user_before={'cash': user.cash, 'debt': user.debt,
                    'debt_last_accrued_at': user.debt_last_accrued_at},
                short_before=shorts._short_snapshot(target),
                pre_gold=pair.gold_reserve, pre_foreign=pair.foreign_reserve,
                reject=shorts.ShortCoverRejected,
                execution_id=receipt.id, execution_leg=index)
            trade = cover.trade
        elif leg.purpose == 'short_open':
            opened = await shorts._write_open_ledger(db, user=user, pair=pair,
                positions=positions, target=target, treasury=treasury,
                amount=math.input_amount, q=math, now=now,
                daily_rate=simulation.daily_rate, min_out=ZERO, idempotency_key=None,
                execution_id=receipt.id, execution_leg=index)
            trade = opened.trade
        else:
            if wallet is None:
                wallet = FxWallet(user_id=user_id, pair_id=pair_id)
                db.add(wallet)
            borrowed = quote.borrow_amount if leg.purpose == 'borrow_buy' else None
            if borrowed:
                user = await loan_service.increase_debt_locked(db, user, borrowed,
                    grant_cash=True, daily_rate=simulation.daily_rate, now=now,
                    source='borrow', reason=f'fx_net:{pair_id}:{req.idempotency_key}')
            spot = await trading._write_spot_ledger(db, user=user, pair=pair,
                wallet=wallet, treasury=treasury, normalized=leg.side, q=math,
                min_out=ZERO, idempotency_key=None, financed=bool(borrowed),
                borrow_amount=borrowed, trade_now=now,
                execution_id=receipt.id, execution_leg=index)
            trade = spot.trade
        trade.execution_id, trade.execution_leg = receipt.id, index
        trades.append(trade)
    result = FxNetTradeResponse(**quote.model_dump(), execution_id=receipt.id,
        trade_ids=[t.id for t in trades], created_at=now)
    receipt.response_payload = result.model_dump(mode='json')
    trading._require_writes()
    return result


async def execute_net_trade(db, user_id, pair_id, req):
    from app.services.fx.writer import FX_WRITER
    from app.services.fx.writer_ops import NetTrade
    trading._require_writes()
    command = NetTrade(pair_id=int(pair_id), user_id=int(user_id), side=req.side,
        amount=req.amount, borrow_amount=req.borrow_amount, allow_growth=req.allow_growth,
        min_out=req.min_out, idempotency_key=req.idempotency_key,
        expected_economic_version=req.expected_economic_version)
    if db.in_transaction():
        await db.rollback()
    return await FX_WRITER.submit(command)


def _instruction_payload(pair_id, instruction):
    payload = instruction.model_dump(mode='json')
    for name in ('amount', 'close_percent', 'borrow_limit'):
        value = getattr(instruction, name)
        payload[name] = format(value.normalize(), 'f') if value is not None else None
    return {'type': 'instruction', 'pair_id': pair_id, **payload}


async def execute_fx_instruction_in_session(db: AsyncSession, *, user_id: int, pair_id: int,
                                           instruction: FxTradeInstruction, request_key: str,
                                           credit_deps: DependencySet | None = None
                                           ) -> FxNetTradeResponse | shorts.FxShortExecution:
    """Resolve authorized amounts at one locked UTC T and reuse money cores."""
    from app.schemas.fx import FxNetTradeRequest, FxNetTradeResponse
    from app.services.market_locks import lock_user
    key = GroupKey('fx', pair_id)
    pair = await trading._pair(db, pair_id, lock=True)
    user = await lock_user(db, user_id)
    payload = _instruction_payload(pair_id, instruction)
    old, legacy = await _read_net_request_identity(db, user_id, request_key)
    if old:
        if old.request_payload != payload:
            raise HTTPException(status_code=409, detail='idempotency key parameter mismatch')
        if instruction.action == 'close_short':
            trade = await db.get(FxTrade, old.response_payload['trade_id'])
            return shorts.FxShortExecution(trade=trade, replay=True, pair_id=pair_id,
                post_price=Decimal(trade.post_price), trade_id=trade.id)
        return FxNetTradeResponse.model_validate(old.response_payload).model_copy(update={'replay': True})
    if legacy:
        raise HTTPException(status_code=409, detail='idempotency key parameter mismatch')
    now = trading.utcnow()
    if instruction.action == 'close_short':
        if credit_deps is None:
            raise trading._RetryCredit()
        result = await shorts.execute_short_cover_in_session(db, user_id=user_id,
            pair_id=pair_id, foreign_amount=None, cover_all=instruction.close_percent == 100,
            max_gold_in=ZERO, idempotency_key=request_key, credit_deps=credit_deps,
            _close_percent=instruction.close_percent, _now=now)
        db.add(FxExecution(user_id=user_id, pair_id=pair_id, idempotency_key=request_key,
            request_payload=payload, response_payload={'trade_id': result.trade_id}, created_at=now))
        await db.flush()
        return result
    amount = instruction.amount
    if instruction.action == 'close_long':
        wallet = await trading._wallet_lock(db, user_id, pair_id, create=False)
        holdings = Decimal(wallet.foreign_amount) if wallet else ZERO
        if holdings <= 0:
            raise HTTPException(status_code=400, detail='no long position to close')
        amount = amount_down(holdings * instruction.close_percent / 100)
        if amount <= 0:
            raise HTTPException(status_code=400, detail='long close quantity is too small')
    req = FxNetTradeRequest(side='buy' if instruction.action == 'buy' else 'sell',
        amount=amount, borrow_amount=ZERO, allow_growth=instruction.action != 'close_long',
        min_out=ZERO, idempotency_key=request_key, expected_economic_version=economic_version_of(user))
    if instruction.auto_borrow and credit_deps is None:
        raise trading._RetryCredit()
    return await execute_net_trade_in_session(db, user_id=user_id, pair_id=pair_id,
        req=req, credit_deps=credit_deps, _instruction=instruction, _now=now)
