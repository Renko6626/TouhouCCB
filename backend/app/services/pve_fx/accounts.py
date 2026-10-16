"""FX account lifecycle. Caller owns the transaction; no pool mutations here."""
from decimal import Decimal, ROUND_DOWN
import random

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import User
from app.models.bot import BotProfile, BOT_STATUS_PAUSED, BOT_STATUS_RETIRED
from app.models.fx import FxPair, FxWallet, FxShortPosition, FxTrade, FxExecution, FxTriggerOrder
from app.schemas.pve_fx import (
    FX_AMOUNT_MAX, FxPveGenerateRequest, FxPvePatchRequest, FxPveFundRequest, FxPveBotItem,
)
from app.services import audit_service
from app.services.credit.ownership import OWNERSHIP
from app.services.credit.version import bump_economic_version
from app.services.fx.amm import marginal_price, quote_sell
from app.services.ledger_service import record_entry
from app.services.pve.naming import generate_usernames
from app.services.pve.service import BotOpError, fund_bot, destroy_bot
from .presets import PRESETS
from .registry import get_strategy, validate_strategy_params
from .types import FxBotValues

ZERO = Decimal('0')
Q6 = Decimal('.000001')


async def _profile(db, profile_id: int, *, lock=False) -> BotProfile:
    stmt = select(BotProfile).where(BotProfile.id == profile_id, BotProfile.market_kind == 'fx')
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    profile = (await db.execute(stmt)).scalars().first()
    if profile is None:
        raise BotOpError(404, 'FX 机器人不存在')
    return profile


def fx_bot_value(cash: Decimal, amount: Decimal, pair: FxPair) -> FxBotValues:
    cash, amount = Decimal(cash), Decimal(amount)
    mtm = amount * marginal_price(pair.gold_reserve, pair.foreign_reserve)
    proceeds = ZERO
    if amount > ZERO:
        try:
            proceeds = quote_sell(amount, pair.gold_reserve, pair.foreign_reserve, pair.sell_fee_rate).output_amount
        except ValueError:
            # Dust or fees consuming the input have no executable proceeds.
            pass
    return FxBotValues(cash=cash, wallet=amount, mtm=mtm, liquidation_cash=proceeds)


async def read_fx_bot_values(db: AsyncSession, profile_ids: list[int]) -> dict[int, FxBotValues]:
    rows = (await db.execute(
        select(BotProfile, User, FxPair).join(User, User.id == BotProfile.user_id)
        .join(FxPair, FxPair.id == BotProfile.fx_pair_id)
        .where(BotProfile.id.in_(profile_ids), BotProfile.market_kind == 'fx')
    )).all()
    wallets = (await db.execute(select(FxWallet).where(
        FxWallet.user_id.in_([u.id for _, u, _ in rows])
    ))).scalars().all()
    balances = {(w.user_id, w.pair_id): w.foreign_amount for w in wallets}
    return {profile.id: fx_bot_value(user.cash, balances.get((user.id, pair.id), ZERO), pair)
            for profile, user, pair in rows}


async def fx_bot_item(db: AsyncSession, profile: BotProfile) -> FxPveBotItem:
    user = await db.get(User, profile.user_id)
    value = (await read_fx_bot_values(db, [profile.id]))[profile.id]
    return FxPveBotItem(
        profile_id=profile.id, user_id=user.id, username=user.username,
        pair_id=profile.fx_pair_id, template=profile.template, params=profile.params,
        status=profile.status, strategy_state=profile.strategy_state,
        inventory_baseline_foreign=Decimal(profile.params['inventory_baseline_foreign']),
        cash=value.cash, wallet=value.wallet, mtm=value.mtm, liquidation_cash=value.liquidation_cash,
    )


async def generate_fx_bots(db: AsyncSession, request: FxPveGenerateRequest, operator_id: int) -> list[FxPveBotItem]:
    OWNERSHIP.require_writes()
    pair = (await db.execute(select(FxPair).where(FxPair.id == request.pair_id)
        .with_for_update().execution_options(populate_existing=True))).scalars().first()
    if pair is None or pair.archived or pair.status != 'trading':
        raise BotOpError(400, '交易对须存在、未归档且正在交易')
    price = marginal_price(pair.gold_reserve, pair.foreign_reserve)
    raw_basis = request.initial_foreign * price
    if raw_basis > FX_AMOUNT_MAX:
        raise BotOpError(400, '初始外币成本超过钱包存储上限')
    basis = raw_basis.quantize(Q6, rounding=ROUND_DOWN)
    # Validate every template before inserting any account.
    preset = PRESETS[request.preset]
    episode_profiles = preset["episode_profiles"]
    params_list = []
    episode_index = 0
    for item in request.items:
        get_strategy(item.template)
        for _ in range(item.count):
            params = {**preset['strategy_params'][item.template], 'inventory_baseline_foreign': str(request.initial_foreign)}
            if item.template == 'episode':
                params.update(episode_profiles[episode_index % len(episode_profiles)])
                episode_index += 1
            params_list.append((item.template, validate_strategy_params(item.template, params)))
    taken = set((await db.execute(select(User.username))).scalars().all())
    names = generate_usernames(request.naming_style, len(params_list), taken, random.Random())
    created = []
    for name, (template, params) in zip(names, params_list):
        OWNERSHIP.require_writes()
        user = User(username=name, is_bot=True, is_active=True, cash=ZERO)
        db.add(user)
        await db.flush()
        if request.initial_cash > ZERO:
            user.cash = request.initial_cash
            await record_entry(db, user=user, entry_type='admin_adjust_cash',
                cash_delta=request.initial_cash, debt_delta=ZERO, daily_rate=None,
                operator_user_id=operator_id, reason='FX 机器人初始注资')
        wallet = FxWallet(user_id=user.id, pair_id=pair.id,
            foreign_amount=request.initial_foreign, cost_basis=basis)
        profile = BotProfile(user_id=user.id, market_kind='fx', fx_pair_id=pair.id,
            template=template, params=params, status=BOT_STATUS_PAUSED, market_scope=[], strategy_state={})
        db.add(wallet)
        db.add(profile)
        if request.initial_cash > ZERO or request.initial_foreign > ZERO:
            bump_economic_version(user)
        await db.flush()
        if request.initial_foreign > ZERO:
            audit_service.record(db, 'pve_fx_initial_allocation', user_id=user.id,
                operator_user_id=operator_id, ref_table='fx_wallet', ref_id=wallet.id,
                user_after=audit_service.user_snapshot(user), payload={
                    'pair_id': pair.id, 'foreign_amount': request.initial_foreign,
                    'reference_price': price,
                    'wallet_before': {'foreign_amount': ZERO, 'cost_basis': ZERO},
                    'wallet_after': {'foreign_amount': wallet.foreign_amount, 'cost_basis': basis},
                })
        created.append(await fx_bot_item(db, profile))
    return created


async def patch_fx_bot(db: AsyncSession, profile_id: int, request: FxPvePatchRequest, operator_id: int) -> FxPveBotItem:
    OWNERSHIP.require_writes()
    # Match execution's user-before-profile lock order.
    initial = await _profile(db, profile_id)
    await db.execute(select(User).where(User.id == initial.user_id).with_for_update())
    profile = await _profile(db, profile_id, lock=True)
    if profile.status == BOT_STATUS_RETIRED:
        raise BotOpError(409, '已退休机器人不能修改或恢复')
    changing_template = request.template is not None and request.template != profile.template
    editing = changing_template or request.params is not None
    if editing:
        if (changing_template or profile.template == 'episode') and profile.status != BOT_STATUS_PAUSED:
            raise BotOpError(409, '请先暂停机器人再修改策略')
        if (profile.strategy_state or {}).get('episode') and (changing_template or profile.template == 'episode'):
            raise BotOpError(409, '请先完成短线轮次或清仓再修改策略')
        baseline = profile.params['inventory_baseline_foreign']
        incoming = request.params or {}
        if 'inventory_baseline_foreign' in incoming:
            raise BotOpError(400, '初始外币基准只读')
        template = request.template or profile.template
        params = {} if changing_template else dict(profile.params)
        params.update(incoming)
        params['inventory_baseline_foreign'] = baseline
        profile.params = validate_strategy_params(template, params)
        profile.template = template
        if changing_template:
            profile.strategy_state = {}
    if request.status is not None:
        profile.status = request.status
    await db.flush()
    return await fx_bot_item(db, profile)


async def fund_fx_bot(db: AsyncSession, profile_id: int, amount: Decimal, operator_id: int, reason: str | None = None) -> FxPveBotItem:
    request = FxPveFundRequest(amount=amount, reason=reason)
    OWNERSHIP.require_writes()
    initial = await _profile(db, profile_id)
    await db.execute(select(User).where(User.id == initial.user_id).with_for_update())
    profile = await _profile(db, profile_id, lock=True)
    await fund_bot(db, profile=profile, amount=request.amount, operator_user_id=operator_id, reason=request.reason)
    await db.flush()
    return await fx_bot_item(db, profile)


async def retire_fx_bot(db: AsyncSession, profile_id: int, operator_id: int) -> dict:
    OWNERSHIP.require_writes()
    initial = await _profile(db, profile_id)
    await db.execute(select(User).where(User.id == initial.user_id).with_for_update())
    profile = await _profile(db, profile_id, lock=True)
    if (profile.strategy_state or {}).get('episode'):
        raise BotOpError(409, '短线轮次尚未结束，请先清仓')
    short = (await db.execute(select(FxShortPosition).where(
        FxShortPosition.user_id == profile.user_id,
        (FxShortPosition.principal_foreign + FxShortPosition.interest_foreign) > ZERO,
    ))).scalars().first()
    if short is not None:
        raise BotOpError(409, '仍有外币债务，请先偿还后退休')
    has_trade = (await db.execute(select(FxTrade.id).where(FxTrade.user_id == profile.user_id).limit(1))).first()
    if not has_trade:
        for model in (FxShortPosition, FxExecution, FxTriggerOrder):
            reference = (await db.execute(select(model.id).where(model.user_id == profile.user_id).limit(1))).first()
            if reference:
                raise BotOpError(409, '账户仍有业务记录，不能作为空白账户删除')
    return await destroy_bot(db, profile=profile, operator_user_id=operator_id)
