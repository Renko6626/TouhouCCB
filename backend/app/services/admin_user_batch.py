"""Explicit, bounded administrative account operations through the product writers."""
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from app.models.base import Market, Outcome, Position, User
from app.models.bot import BotProfile
from app.models.credit import LiquidationRun
from app.models.fx import FxPair, FxShortPosition, FxTriggerOrder, FxWallet
from app.services import admin_user_service as accounts, audit_service, loan_service, site_config
from app.services.credit import runs
from app.services.credit.cash import has_foreign_debt
from app.services.credit.fx_quote import FxPairSnapshot, quote_fx_group
from app.services.credit.lmsr_quote import OutcomeSnapshot, quote_lmsr_group
from app.services.credit.ownership import OWNERSHIP
from app.services.credit.risk import discover_dependencies
from app.services.fx import shorts, trading
from app.services.fx.amm import marginal_price, quote_buy_exact_out

D = Decimal
ZERO = D('0')
ONE = D('1')


def _now():
    return datetime.now(timezone.utc)


def _trade(product, group_id, side, quantity, gold, **extra):
    return dict(product=product, group_id=group_id, side=side,
                quantity=float(quantity), gold=float(gold), **extra)


async def validate_locked_target(db, target, admin_id, reason):
    """Called while the writer owns the target user lock, before financial writes."""
    if not reason or not reason.strip():
        raise accounts.AdminUserError(422, '请填写操作原因')
    actor = (await db.execute(select(User).where(User.id == admin_id)
        .execution_options(populate_existing=True))).scalar_one_or_none()
    if actor is None or not actor.is_active or not actor.is_superuser:
        raise accounts.AdminUserError(403, '管理员权限已失效')
    if target.id == admin_id:
        raise accounts.AdminUserError(400, '不能对自己执行批量账户操作')
    if target.is_superuser:
        raise accounts.AdminUserError(400, '批量操作不包含管理员')
    bot = target.is_bot or (await db.execute(select(BotProfile.id).where(
        BotProfile.user_id == target.id))).first() is not None
    if bot:
        raise accounts.AdminUserError(400, '批量操作不包含官方机器人')


async def _group_ids(db, user_id):
    markets = list((await db.execute(select(Outcome.market_id).join(
        Position, Position.outcome_id == Outcome.id).where(
            Position.user_id == user_id, Position.amount > ZERO).distinct()
        .order_by(Outcome.market_id))).scalars())
    pair_ids = set((await db.execute(select(FxWallet.pair_id).where(
        FxWallet.user_id == user_id, FxWallet.foreign_amount > ZERO))).scalars())
    pair_ids.update((await db.execute(select(FxShortPosition.pair_id).where(
        FxShortPosition.user_id == user_id,
        FxShortPosition.principal_foreign + FxShortPosition.interest_foreign > ZERO))).scalars())
    pair_ids.update((await db.execute(select(FxTriggerOrder.pair_id).where(
        FxTriggerOrder.user_id == user_id, FxTriggerOrder.status == 'waiting'))).scalars())
    return markets, sorted(pair_ids)


async def close_fx_in_session(db, cmd):
    """One pair's sale and full cover commit together; partial covers raise/rollback."""
    OWNERSHIP.require_writes()
    pair = await trading._pair(db, cmd.pair_id, lock=True)
    user = await accounts._lock_user(db, cmd.user_id)
    await validate_locked_target(db, user, cmd.admin_id, cmd.reason)
    if pair.archived:
        raise accounts.AdminUserError(409, '已归档的货币对无法平仓')
    run = await runs.get_or_create_active_run(db, user_id=user.id,
        trigger_source='admin_close', now=_now())
    trades = []
    wallet = (await db.execute(select(FxWallet).where(FxWallet.user_id == user.id,
        FxWallet.pair_id == pair.id))).scalar_one_or_none()
    if wallet is not None and wallet.foreign_amount > ZERO:
        result = await trading.execute_liquidation_sell_in_session(db,
            user_id=user.id, pair_id=pair.id, run_id=run.id,
            round_no=run.next_round, mode='full')
        if result.blocked_reason:
            raise accounts.AdminUserError(409, result.blocked_reason)
        q = result.quote
        await runs.record_action(db, run=run, round_no=run.next_round,
            kind='sell_group', product='fx', group_id=pair.id, mode='full',
            proceeds=q.gold_out, fee=q.fee_foreign, fee_currency='foreign',
            cash_after=user.cash, debt_after=user.debt,
            economic_version_after=user.economic_version)
        trades.append(_trade('fx', pair.id, 'sell', q.foreign_in, q.gold_out,
                             trade_id=result.public.id))
    position = (await db.execute(select(FxShortPosition).where(
        FxShortPosition.user_id == user.id, FxShortPosition.pair_id == pair.id))).scalar_one_or_none()
    if position is not None and position.principal_foreign + position.interest_foreign > ZERO:
        deps = await discover_dependencies(db, user.id, account_locked=True, locked_user=user)
        result = await shorts.execute_liquidation_cover_in_session(db,
            user_id=user.id, pair_id=pair.id, run_id=run.id, round_no=run.next_round,
            planned_amount=shorts._MAX_DEBT, max_gold_budget=user.cash, credit_deps=deps)
        if result.blocked_reason or not result.full_cover:
            raise accounts.AdminUserError(409, result.blocked_reason or '现金不足，无法完整回补；本货币对未成交')
        await runs.record_action(db, run=run, round_no=run.next_round,
            kind='cover_group', product='fx', group_id=pair.id, mode='full',
            gold_spent=result.paid_gold, foreign_repaid=result.repaid_foreign,
            fee=result.fee_gold, fee_currency='gold', cash_after=user.cash,
            debt_after=user.debt, economic_version_after=user.economic_version)
        trades.append(_trade('fx', pair.id, 'cover', result.repaid_foreign,
                             result.paid_gold, trade_id=result.trade_id))
    orders = list((await db.execute(select(FxTriggerOrder).where(
        FxTriggerOrder.user_id == user.id, FxTriggerOrder.pair_id == pair.id,
        FxTriggerOrder.status == 'waiting').with_for_update())).scalars())
    from app.services.fx.orders import cancel_order_in_session
    for order in orders:
        await cancel_order_in_session(db, user.id, order.id)
    if trades and user.auto_repay and user.debt > ZERO and not await has_foreign_debt(db, user.id):
        rate = await site_config.get_decimal(db, 'loan_daily_rate')
        debt_before = user.debt
        now = loan_service._compat_now(user)
        loan_service.accrue_interest(user, rate, now)
        paid = await loan_service.decrease_debt_locked(db, user, min(user.cash, user.debt),
            consume_cash=True, daily_rate=rate, now=now)
        audit_service.record_liquidation_repay(db, user, paid, debt_before, rate, 'admin_close')
    audit_service.record(db, 'liquidation_action', user_id=user.id,
        operator_user_id=cmd.admin_id, payload={'reason': cmd.reason, 'product': 'fx',
            'group_id': pair.id, 'run_id': run.id, 'cancelled_orders': len(orders),
            'trades': trades})
    return {'trades': trades, 'cash_after': float(user.cash),
        'post_price': str(marginal_price(pair.gold_reserve, pair.foreign_reserve))}


async def _preview_close(db, user, rolling_fx, rolling_q, rate, fee):
    cash = D(user.cash)
    debt = loan_service.pending_debt(user, rate, _now())
    markets, pairs = await _group_ids(db, user.id)
    has_shorts = await has_foreign_debt(db, user.id)
    trades = []
    for mid in markets:
        market = await db.get(Market, mid)
        outcomes = list((await db.execute(select(Outcome).where(Outcome.market_id == mid)
            .order_by(Outcome.id))).scalars())
        held = dict((await db.execute(select(Position.outcome_id, Position.amount).where(
            Position.user_id == user.id, Position.outcome_id.in_([o.id for o in outcomes])))).all())
        q = rolling_q.setdefault(mid, {o.id: D(o.total_shares) for o in outcomes})
        quote = quote_lmsr_group([OutcomeSnapshot(o.id, q[o.id], str(getattr(market.status, 'value', market.status)),
            market.closes_at) for o in outcomes], held, market_id=mid,
            b=float(market.liquidity_b), fee_rate=fee, mode='full', partial_pct=ONE)
        if quote.blocked_reason or len(quote.legs) != len(held):
            raise accounts.AdminUserError(409, quote.blocked_reason or '存在无法卖出的预测持仓')
        for leg in quote.legs:
            q[leg.outcome_id] -= leg.amount
            cash += leg.net
            trades.append(_trade('lmsr', mid, 'sell', leg.amount, leg.net))
        if user.auto_repay and not has_shorts:
            paid = min(cash, debt)
            cash -= paid
            debt -= paid
    for pid in pairs:
        pair = await db.get(FxPair, pid)
        if pair is None or pair.archived:
            raise accounts.AdminUserError(409, '货币对不存在或已归档')
        original = rolling_fx.setdefault(pid, [D(pair.gold_reserve), D(pair.foreign_reserve), pair.currency_name])
        gold, foreign = original[:2]
        next_cash = cash
        legs = []
        wallet = (await db.execute(select(FxWallet).where(FxWallet.user_id == user.id,
            FxWallet.pair_id == pid))).scalar_one_or_none()
        if wallet is not None and wallet.foreign_amount > ZERO:
            quote = quote_fx_group(FxPairSnapshot(pid, pair.status, pair.reduce_only,
                gold, foreign, pair.sell_fee_rate), foreign_amount=wallet.foreign_amount,
                mode='full', partial_pct=ONE)
            if quote.blocked_reason:
                raise accounts.AdminUserError(409, quote.blocked_reason)
            gold, foreign = quote.post_gold_reserve, quote.post_foreign_reserve
            next_cash += quote.gold_out
            legs.append(_trade('fx', pid, 'sell', quote.foreign_in, quote.gold_out))
        short = (await db.execute(select(FxShortPosition).where(FxShortPosition.user_id == user.id,
            FxShortPosition.pair_id == pid))).scalar_one_or_none()
        if short is not None and short.principal_foreign + short.interest_foreign > ZERO:
            if pair.status != 'trading' and not (pair.status == 'paused' and pair.reduce_only):
                raise accounts.AdminUserError(409, '货币对暂停，无法回补')
            amount = shorts.pending_short_debt(short, rate, _now())
            try:
                quote = quote_buy_exact_out(amount, gold, foreign, pair.buy_fee_rate)
            except (ValueError, ArithmeticError) as exc:
                raise accounts.AdminUserError(409, '储备不足，无法完整回补') from exc
            if quote.input_amount > next_cash:
                raise accounts.AdminUserError(409, '现金不足，无法完整回补')
            gold, foreign = quote.post_gold_reserve, quote.post_foreign_reserve
            next_cash -= quote.input_amount
            legs.append(_trade('fx', pid, 'cover', amount, quote.input_amount))
        rolling_fx[pid] = [gold, foreign, pair.currency_name]
        cash = next_cash
        trades.extend(legs)
    if user.auto_repay and trades:
        cash -= min(cash, debt)
    return trades, cash


async def _execute_close(db, user_id, admin_id, reason, trades):
    from app.services.fx.writer import FX_WRITER
    from app.services.fx.writer_ops import AdminCloseAccount
    from app.services.market_writer import WRITER
    from app.services.writer_ops import AdminCloseGroupCmd
    markets, pairs = await _group_ids(db, user_id)
    fee = await site_config.get_decimal(db, 'sell_fee_rate')
    rate = await site_config.get_decimal(db, 'loan_daily_rate')
    await db.close()
    for mid in markets:
        result = await WRITER.submit(AdminCloseGroupCmd(market_id=mid, user_id=user_id,
            run_id=0, round_no=1, mode='full', partial_pct=ONE, fee_rate=fee,
            daily_rate=rate, trigger_source='admin_close', admin_id=admin_id, admin_reason=reason))
        if result.get('blocked_reason'):
            raise accounts.AdminUserError(409, result['blocked_reason'])
        if result['sold_count']:
            trades.append(_trade('lmsr', mid, 'sell', result['quantity'], result['net']))
    for pid in pairs:
        result = await FX_WRITER.submit(AdminCloseAccount(pid, user_id, admin_id, reason))
        trades.extend(result['trades'])
    async with db.begin():
        user = await accounts._lock_user(db, user_id)
        await validate_locked_target(db, user, admin_id, reason)
        remaining_markets, remaining_pairs = await _group_ids(db, user_id)
        if remaining_markets or remaining_pairs:
            raise accounts.AdminUserError(409, '仍有持仓或待执行订单，请刷新后检查（账户可能有新的交易）')
        active = (await db.execute(select(LiquidationRun).where(
            LiquidationRun.user_id == user_id, LiquidationRun.status == 'active')
            .with_for_update())).scalar_one_or_none()
        if active is not None:
            await runs.close_run(db, run=active, status='stopped', now=_now())
        cash, debt = float(user.cash), float(user.debt)
    return cash, debt


async def run_batch(db, *, user_ids: list[int], action: str, reason: str,
                    dry_run: bool, admin_id: int) -> dict:
    ids = list(dict.fromkeys(user_ids))
    if not ids or len(ids) > 100 or any(type(uid) is not int or uid <= 0 for uid in ids):
        raise accounts.AdminUserError(422, '请选择 1 至 100 个有效用户 ID')
    reason = reason.strip()
    if not reason or action not in {'ban', 'unban', 'close', 'ban_and_close'}:
        raise accounts.AdminUserError(422, '操作或操作原因无效')
    if not dry_run:
        OWNERSHIP.require_writes()
    rolling_fx, rolling_q, before = {}, {}, {}
    items = []
    rate = await site_config.get_decimal(db, 'loan_daily_rate') if action in {'close', 'ban_and_close'} else ZERO
    fee = await site_config.get_decimal(db, 'sell_fee_rate') if action in {'close', 'ban_and_close'} else ZERO
    for uid in ids:
        fx_before = {pid: list(values) for pid, values in rolling_fx.items()}
        q_before = {mid: dict(values) for mid, values in rolling_q.items()}
        item = {'user_id': uid, 'username': '', 'status': 'failed', 'message': '', 'trades': []}
        items.append(item)
        try:
            user = (await db.execute(select(User).where(User.id == uid)
                .execution_options(populate_existing=True))).scalar_one_or_none()
            if user is None:
                raise accounts.AdminUserError(404, '用户不存在')
            item['username'] = user.username
            await validate_locked_target(db, user, admin_id, reason)
            item['cash_before'] = float(user.cash)
            is_active = user.is_active
            # Materialize starting prices before preview mutates its rolling copy.
            if action in {'close', 'ban_and_close'}:
                _, pids = await _group_ids(db, uid)
                for pid in pids:
                    pair = await db.get(FxPair, pid)
                    if pair is not None:
                        before.setdefault(pid, marginal_price(pair.gold_reserve, pair.foreign_reserve))
            if dry_run:
                if action in {'close', 'ban_and_close'}:
                    item['trades'], cash = await _preview_close(db, user, rolling_fx, rolling_q, rate, fee)
                    item['cash_after'] = float(cash)
                elif is_active == (action == 'unban'):
                    item.update(status='skipped', message='账户已处于目标状态')
                    continue
                item.update(status='ready', message='可执行')
                continue
            await db.rollback()
            if action in {'ban', 'ban_and_close'}:
                target = await accounts._lock_user(db, uid)
                await validate_locked_target(db, target, admin_id, reason)
                await accounts.ban(db, target_id=uid, admin_id=admin_id, reason=reason)
                item['is_active'] = False
            elif action == 'unban':
                target = await accounts._lock_user(db, uid)
                await validate_locked_target(db, target, admin_id, reason)
                await accounts.unban(db, target_id=uid, admin_id=admin_id, reason=reason)
                item['is_active'] = True
            if action in {'close', 'ban_and_close'}:
                cash, debt = await _execute_close(db, uid, admin_id, reason, item['trades'])
                item.update(cash_after=cash, debt_after=debt)
                item['message'] = '持仓已平；剩余金债请在贷款记录中查看' if debt else '持仓已平'
            else:
                item['message'] = '已封禁' if action == 'ban' else '已解封'
            item['status'] = 'success'
        except (accounts.AdminUserError, shorts.ShortRetryCredit, ValueError, ArithmeticError) as exc:
            if dry_run:
                rolling_fx, rolling_q = fx_before, q_before
            item['message'] = getattr(exc, 'detail', str(exc)) or '账户或行情已变化，请刷新后重试'
            if not dry_run and action == 'ban_and_close' and item.get('is_active') is False:
                item['message'] = '已封禁，平仓未完成：' + item['message']
            await db.rollback()
        except Exception as exc:
            from fastapi import HTTPException
            if not isinstance(exc, HTTPException):
                raise
            item['message'] = str(exc.detail)
            if not dry_run and action == 'ban_and_close' and item.get('is_active') is False:
                item['message'] = '已封禁，平仓未完成：' + item['message']
            await db.rollback()
    impacts = []
    for pid, price in before.items():
        if dry_run:
            if pid not in rolling_fx:
                continue
            gold, foreign, name = rolling_fx[pid]
        else:
            pair = (await db.execute(select(FxPair).where(FxPair.id == pid)
                .execution_options(populate_existing=True))).scalar_one()
            gold, foreign, name = pair.gold_reserve, pair.foreign_reserve, pair.currency_name
        impacts.append(dict(pair_id=pid, currency_name=name, price_before=float(price),
            price_after=float(marginal_price(gold, foreign))))
    return {'dry_run': dry_run, 'items': items, 'price_impacts': impacts}
