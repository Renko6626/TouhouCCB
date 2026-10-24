"""Read-only projections for the administrator user hub."""
from datetime import datetime, timezone

from sqlalchemy import func, or_, select

from app.models.audit import AuditEvent
from app.models.base import Market, Outcome, Position, Transaction, User
from app.models.fx import FxPair, FxShortPosition, FxTrade, FxWallet
from app.services import site_config
from app.services.admin_user_service import AdminUserError
from app.services.fx.shorts import pending_short_debt


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def _latest(*values):
    return max((_utc(v) for v in values if v is not None), default=None)


def _user_metrics():
    spot = select(func.count(FxWallet.id)).where(
        FxWallet.user_id == User.id, FxWallet.foreign_amount > 0).correlate(User).scalar_subquery()
    short = select(func.count(FxShortPosition.id)).where(
        FxShortPosition.user_id == User.id,
        FxShortPosition.principal_foreign + FxShortPosition.interest_foreign > 0,
    ).correlate(User).scalar_subquery()
    lmsr = select(func.count(Position.id)).where(
        Position.user_id == User.id, Position.amount > 0).correlate(User).scalar_subquery()
    registered = select(func.min(AuditEvent.ts)).where(
        AuditEvent.user_id == User.id, AuditEvent.event_type == 'user_register',
    ).correlate(User).scalar_subquery()
    fx_last = select(func.max(FxTrade.created_at)).where(
        FxTrade.user_id == User.id).correlate(User).scalar_subquery()
    lmsr_last = select(func.max(Transaction.timestamp)).where(
        Transaction.user_id == User.id).correlate(User).scalar_subquery()
    return spot, short, lmsr, registered, fx_last, lmsr_last


async def list_users(db, *, offset=0, limit=200, q=None, user_ids=None,
                     is_active=None, has_positions=None, has_shorts=None, include_bots=True):
    spot, short, lmsr, registered, fx_last, lmsr_last = _user_metrics()
    stmt = select(User, spot, short, lmsr, registered, fx_last, lmsr_last)
    term = (q or '').strip()
    if term:
        matches = [User.username.ilike(f'%{term}%')]
        if term.isdigit() and int(term) <= 2147483647:
            matches.append(User.id == int(term))
        stmt = stmt.where(or_(*matches))
    if user_ids is not None:
        stmt = stmt.where(User.id.in_(user_ids))
    if is_active is not None:
        stmt = stmt.where(User.is_active == is_active)
    if not include_bots:
        stmt = stmt.where(User.is_bot.is_(False))
    if has_positions is not None:
        stmt = stmt.where((spot + short + lmsr > 0) == has_positions)
    if has_shorts is not None:
        stmt = stmt.where((short > 0) == has_shorts)
    rows = (await db.execute(stmt.order_by(User.id).offset(max(0, offset)).limit(min(1000, max(1, limit))))).all()
    return [dict(id=u.id, username=u.username, cash=float(u.cash), debt=float(u.debt),
                 is_active=u.is_active, is_superuser=u.is_superuser, is_bot=u.is_bot,
                 spot_count=s, short_count=h, lmsr_count=l, registered_at=_utc(r),
                 last_trade_at=_latest(f, t)) for u, s, h, l, r, f, t in rows]


async def get_activity(db, user_id, limit=50):
    user = await db.get(User, user_id)
    if user is None:
        raise AdminUserError(404, '用户不存在')
    limit = min(200, max(1, limit))
    positions = []
    wallets = (await db.execute(select(FxWallet, FxPair).join(FxPair, FxPair.id == FxWallet.pair_id).where(
        FxWallet.user_id == user_id, FxWallet.foreign_amount > 0).order_by(FxPair.id))).all()
    for wallet, pair in wallets:
        positions.append(dict(product='fx', group_id=pair.id, market_name=pair.currency_name,
                              asset_name=pair.currency_name, kind='spot', quantity=float(wallet.foreign_amount),
                              interest=0.0, cost_basis=float(wallet.cost_basis)))
    shorts = (await db.execute(select(FxShortPosition, FxPair).join(FxPair, FxPair.id == FxShortPosition.pair_id).where(
        FxShortPosition.user_id == user_id,
        FxShortPosition.principal_foreign + FxShortPosition.interest_foreign > 0).order_by(FxPair.id))).all()
    if shorts:
        rate = await site_config.get_decimal(db, 'loan_daily_rate')
        now = datetime.now(timezone.utc)
        for position, pair in shorts:
            total = pending_short_debt(position, rate, now)
            positions.append(dict(product='fx', group_id=pair.id, market_name=pair.currency_name,
                                  asset_name=pair.currency_name, kind='short', quantity=float(position.principal_foreign),
                                  interest=float(total - position.principal_foreign), cost_basis=float(position.proceeds_basis_gold)))
    lmsr = (await db.execute(select(Position, Outcome, Market).join(Outcome, Outcome.id == Position.outcome_id)
                            .join(Market, Market.id == Outcome.market_id).where(
                                Position.user_id == user_id, Position.amount > 0).order_by(Market.id, Outcome.id))).all()
    for position, outcome, market in lmsr:
        positions.append(dict(product='lmsr', group_id=market.id, market_name=market.title,
                              asset_name=outcome.label, kind='spot', quantity=float(position.amount),
                              interest=0.0, cost_basis=float(position.cost_basis)))
    fx_trades = (await db.execute(select(FxTrade, FxPair.currency_name).join(FxPair, FxPair.id == FxTrade.pair_id)
                                .where(FxTrade.user_id == user_id).order_by(FxTrade.created_at.desc(), FxTrade.id.desc()).limit(limit))).all()
    transactions = [dict(id=t.id, product='fx', created_at=_utc(t.created_at), market_name=name,
                         side=t.side, purpose=t.purpose,
                         quantity=float(t.output_amount if t.side == 'buy' else t.input_amount),
                         gold=float(t.input_amount if t.side == 'buy' else -t.output_amount),
                         source=t.source) for t, name in fx_trades]
    lmsr_trades = (await db.execute(select(Transaction, Market.title).join(Outcome, Outcome.id == Transaction.outcome_id)
                                  .join(Market, Market.id == Outcome.market_id).where(Transaction.user_id == user_id)
                                  .order_by(Transaction.timestamp.desc(), Transaction.id.desc()).limit(limit))).all()
    transactions.extend(dict(id=t.id, product='lmsr', created_at=_utc(t.timestamp), market_name=name,
                             side=t.type, purpose=t.type, quantity=float(t.shares), gold=float(t.cost), source='lmsr')
                        for t, name in lmsr_trades)
    transactions.sort(key=lambda t: (t['created_at'], t['product'], t['id']), reverse=True)
    transactions = transactions[:limit]
    history = (await db.execute(select(AuditEvent).where(
        AuditEvent.user_id == user_id,
        or_(AuditEvent.event_type.startswith('admin_'),
            AuditEvent.event_type.in_(['credit_freeze_set', 'liquidation_action']) & AuditEvent.operator_user_id.is_not(None)),
    ).order_by(AuditEvent.ts.desc(), AuditEvent.id.desc()).limit(limit))).scalars().all()
    registered = (await db.execute(select(func.min(AuditEvent.ts)).where(
        AuditEvent.user_id == user_id, AuditEvent.event_type == 'user_register'))).scalar_one()
    return dict(positions=positions, transactions=transactions,
                history=[dict(id=e.id, created_at=_utc(e.ts), event_type=e.event_type,
                              operator_user_id=e.operator_user_id, reason=e.payload.get('reason', '')) for e in history],
                registered_at=_utc(registered), last_trade_at=transactions[0]['created_at'] if transactions else None)
