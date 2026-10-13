"""Bounded, clean-session account reads; no valuation or transaction admission."""
from decimal import Decimal
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from app.models.base import User, Position, Outcome
from app.models.fx import FxWallet, FxShortPosition
from app.schemas.trading_context import AccountBasis, GroupKey, GroupCoverage, TradingContext, TradingVersionCheck, TradingRules
from app.services import site_config
from app.services.credit import flags
from app.services.trading_prices import REGISTRY, decimal_string, clock_string, now_string, input_hash

async def read_rules(db):
    config = {}
    for name, key in [('loan_daily_rate', 'loan_daily_rate'), ('lmsr_sell_fee_rate', 'sell_fee_rate')]:
        config[name] = decimal_string(await site_config.get_decimal_or(db, key, Decimal('0')))
    for key in ('fx_enabled', 'fx_short_enabled', 'loan_enabled'):
        config[key] = await site_config.get_bool_or(db, key, False)
    effective = flags.get_flags()
    thresholds = effective.thresholds
    await flags.refresh_new_risk_frozen(db)
    config.update(unified_credit_enabled=True, credit_new_risk_frozen=flags.new_risk_frozen(), risk_engine_ready=effective.risk_engine_ready,
                  leverage=decimal_string(thresholds.leverage) if thresholds else None,
                  initial_ratio=decimal_string(thresholds.r_initial) if thresholds else None,
                  maintenance_ratio=decimal_string(thresholds.r_maintenance) if thresholds else None)
    return TradingRules(rule_version=input_hash(config), **config)

async def authoritative_version(db, user_id):
    version = (await db.execute(select(User.economic_version).where(User.id == user_id))).scalar_one_or_none()
    if version is None:
        raise HTTPException(404, 'account_not_found')
    return str(version)

async def read_dependencies(db, user_id, current_group=None):
    # Only identities: version polling never loads amounts or runs valuation.
    markets = (await db.execute(select(Outcome.market_id).join(Position, Position.outcome_id == Outcome.id).where(Position.user_id == user_id))).scalars().all()
    wallets = (await db.execute(select(FxWallet.pair_id).where(FxWallet.user_id == user_id))).scalars().all()
    shorts = (await db.execute(select(FxShortPosition.pair_id).where(FxShortPosition.user_id == user_id))).scalars().all()
    keys = {('lmsr', i) for i in markets} | {('fx', i) for i in wallets + shorts}
    if current_group:
        keys.add((current_group.product, current_group.id))
    return [GroupKey(product=p, id=i) for p, i in sorted(keys)]

async def read_basis(db, user_id):
    names = ('auto_repay', 'cash', 'debt', 'debt_last_accrued_at', 'credit_frozen', 'is_active', 'is_bot', 'tos_accepted_at', 'last_liquidated_at', 'economic_version')
    row = (await db.execute(select(*(getattr(User, n) for n in names)).where(User.id == user_id))).one()
    values = dict(zip(names, row))
    for name in ('cash', 'debt', 'economic_version'):
        values[name] = decimal_string(values[name])
    for name in ('debt_last_accrued_at', 'tos_accepted_at', 'last_liquidated_at'):
        values[name] = clock_string(values[name])
    positions = (await db.execute(select(Outcome.market_id, Position.outcome_id, Position.amount, Position.cost_basis).outerjoin(Outcome, Outcome.id == Position.outcome_id).where(Position.user_id == user_id).order_by(Position.outcome_id))).all()
    wallets = (await db.execute(select(FxWallet.pair_id, FxWallet.foreign_amount, FxWallet.cost_basis).where(FxWallet.user_id == user_id).order_by(FxWallet.pair_id))).all()
    names_short = ('pair_id', 'principal_foreign', 'interest_foreign', 'interest_last_accrued_at', 'restricted_gold', 'proceeds_basis_gold')
    shorts = (await db.execute(select(*(getattr(FxShortPosition, n) for n in names_short)).where(FxShortPosition.user_id == user_id).order_by(FxShortPosition.pair_id))).all()
    return AccountBasis(user_id=user_id, **values,
        lmsr_positions=[dict(market_id=r[0], outcome_id=r[1], amount=decimal_string(r[2]), cost_basis=decimal_string(r[3])) for r in positions],
        fx_wallets=[dict(pair_id=r[0], foreign_amount=decimal_string(r[1]), cost_basis=decimal_string(r[2])) for r in wallets],
        fx_shorts=[dict(zip(names_short, [r[0], decimal_string(r[1]), decimal_string(r[2]), clock_string(r[3]), decimal_string(r[4]), decimal_string(r[5])])) for r in shorts])

def missing_keys(keys, groups):
    found = {(g.product, g.id) for g in groups}
    return [k for k in keys if (k.product, k.id) not in found]

def coverage(groups):
    return [GroupCoverage(product=g.product, id=g.id, stamp=g.stamp) for g in groups]

async def read_trading_context(session, user_id, current_group=None, *, registry=None, session_factory=None):
    factory = session_factory or async_sessionmaker(session.bind, expire_on_commit=False)
    registry = registry or REGISTRY
    for _ in range(3):
        async with factory() as db:
            before = await authoritative_version(db, user_id)
            basis = await read_basis(db, user_id)
            keys = await read_dependencies(db, user_id, current_group)
            rules = await read_rules(db)
        groups = await registry.refresh_groups(keys)
        async with factory() as db:
            after = await authoritative_version(db, user_id)
        if before == basis.economic_version == after:
            return TradingContext(account=basis, rules=rules, groups=groups, minimum_group_stamps=coverage(groups), missing_groups=missing_keys(keys, groups), as_of=now_string(), server_time=now_string())
    raise HTTPException(409, 'account_context_changed')

async def read_trading_versions(session, user_id, current_group=None, *, registry=None, session_factory=None):
    factory = session_factory or async_sessionmaker(session.bind, expire_on_commit=False)
    async with factory() as db:
        version = await authoritative_version(db, user_id)
        keys = await read_dependencies(db, user_id, current_group)
        rules = await read_rules(db)
    groups = await (registry or REGISTRY).refresh_groups(keys)
    return TradingVersionCheck(economic_version=version, rule_version=rules.rule_version, group_stamps=coverage(groups), missing_groups=missing_keys(keys, groups), server_time=now_string())

async def read_trading_prices(session, user_id, keys, current_group=None, *, registry=None):
    async with async_sessionmaker(session.bind, expire_on_commit=False)() as db:
        allowed = {(k.product, k.id) for k in await read_dependencies(db, user_id, current_group)}
    if any((k.product, k.id) not in allowed for k in keys):
        raise HTTPException(403, 'trading_group_not_allowed')
    return await (registry or REGISTRY).refresh_groups(keys)
