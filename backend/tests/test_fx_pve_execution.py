"""Controlled spot transactions, shared economics and fill-state atomicity."""
from datetime import datetime, timezone
from decimal import Decimal as D
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from app.api.v1 import fx
from app.core.database import async_session_maker
from app.core.users import current_active_user
from app.models.audit import AuditEvent
from app.models.base import SiteConfig, User
from app.models.bot import BotProfile
from app.models.fx import FxPair, FxTrade, FxTreasury, FxWallet
from app.schemas.pve_fx import FxPveGenerateRequest, FxPveConfig, FxPairBotConfig
from app.services import audit_replay, site_config
from app.services.fx import trading
from app.services.pve_fx import accounts, executor
from app.services.pve_fx.config import save_fx_config
from app.services.pve_fx.types import FxDecision, FxOrderIntent, FxTickFrame

async def seed(cash='1000', template='random'):
    async with async_session_maker() as db:
        pair = FxPair(currency_code='PVE', currency_name='Test', status='trading', gold_reserve=D('10000'), foreign_reserve=D('10000'), buy_fee_rate=D('.01'), sell_fee_rate=D('.01'))
        db.add(pair)
        await db.flush()
        db.add(FxTreasury(pair_id=pair.id, gold_balance=D('10000'), foreign_balance=D('10000')))
        db.add(SiteConfig(key='fx_enabled', value='true', value_type='bool'))
        operator = User(username='operator', is_superuser=True)
        db.add(operator)
        await db.flush()
        item = (await accounts.generate_fx_bots(db, FxPveGenerateRequest(pair_id=pair.id, items=[{'template':template, 'count':1}], initial_cash=cash, initial_foreign='100'), operator.id))[0]
        p = await db.get(BotProfile, item.profile_id)
        p.status = 'active'
        await save_fx_config(db, FxPveConfig(enabled=True, pair_configs={pair.id:FxPairBotConfig(enabled=True)}), operator.id)
        await db.commit()
        site_config.clear_cache()
        return item.profile_id, item.user_id, pair.id, operator.id

def frame(pid, tick=1):
    return FxTickFrame(tick, pid, datetime.now(timezone.utc), (), D('1'))

def decision(action, amount, state=None):
    return FxDecision(FxOrderIntent(action, D(amount), 'test'), state or {})

async def snapshot(uid, pid):
    async with async_session_maker() as db:
        user, pair = await db.get(User, uid), await db.get(FxPair, pid)
        wallet = (await db.execute(select(FxWallet).where(FxWallet.user_id == uid))).scalars().one()
        treasury = (await db.execute(select(FxTreasury).where(FxTreasury.pair_id == pid))).scalars().one()
        return (user.cash, wallet.foreign_amount, wallet.cost_basis, pair.gold_reserve, pair.foreign_reserve, treasury.gold_balance, treasury.foreign_balance)

@pytest.mark.asyncio
async def test_allocation_real_buy_sell_and_audit_replay():
    profile, uid, pid, _ = await seed()
    bought = await executor.execute_bot_order(profile, decision('buy_foreign', '1.234567'), frame(pid))
    assert bought.rejection is None and bought.fill.output_amount == D('1.234567')
    sold = await executor.execute_bot_order(profile, decision('sell_foreign', '1.234567'), frame(pid, 2))
    assert sold.rejection is None and sold.fill.input_amount == D('1.234567')
    async with async_session_maker() as db:
        replay, errors = audit_replay.fold(await audit_replay.load_events(db), check=True)
        assert errors == []
        assert await audit_replay.compare_with_live(db, replay) == []
        assert (await db.get(BotProfile, profile)).last_trade_at is not None

@pytest.mark.asyncio
async def test_cash_clipped_exact_out_replay_and_mode_identity():
    profile, uid, pid, _ = await seed(cash='1')
    result = await executor.execute_bot_order(profile, decision('buy_foreign', '20'), frame(pid))
    assert result.rejection is None
    assert 0 < result.fill.output_amount < 1 and result.fill.input_amount <= 1
    assert result.fill.requested_amount == 20
    before = await snapshot(uid, pid)
    async def fail(*_): raise AssertionError('callback repeated')
    async with async_session_maker() as db:
        key = (await db.get(FxTrade, result.fill.trade_id)).idempotency_key
        replay = await trading.execute_trade(db, uid, pid, 'buy', D('20'), D('0'), key, bot_profile_id=profile, amount_mode='foreign_out', before_commit=fail)
        assert replay.output_amount == result.fill.output_amount
        with pytest.raises(Exception, match='idempotency key parameter mismatch'):
            await trading.execute_trade(db, uid, pid, 'buy', D('20'), D('0'), key, bot_profile_id=profile, bot_reference_price=D('1'))
    assert await snapshot(uid, pid) == before

@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['paused', 'inactive', 'wrong_pair', 'debt', 'flags'])
async def test_scheduled_admission(invalid):
    profile, uid, pid, _ = await seed()
    async with async_session_maker() as db:
        p, user = await db.get(BotProfile, profile), await db.get(User, uid)
        if invalid == 'paused': p.status = 'paused'
        if invalid == 'inactive': user.is_active = False
        if invalid == 'debt': user.debt = D('1')
        if invalid == 'flags': await save_fx_config(db, FxPveConfig(), uid)
        await db.commit()
    before = await snapshot(uid, pid)
    result = await executor.execute_bot_order(profile, decision('buy_foreign', '1'), frame(pid + (1 if invalid == 'wrong_pair' else 0)))
    assert result.rejection and result.fill is None
    assert await snapshot(uid, pid) == before

@pytest.mark.asyncio
async def test_callback_failure_rolls_back_money_and_preserves_observation(monkeypatch):
    profile, uid, pid, _ = await seed(template='episode')
    episode = {'direction':1, 'entry_tick':0, 'remaining':'10', 'basis':'10', 'realized_profit':'0', 'peak_return':None, 'closing_reason':None}
    async with async_session_maker() as db:
        p = await db.get(BotProfile, profile)
        p.strategy_state = {'episode':episode}
        await db.commit()
    observed = {**episode, 'peak_return':.02, 'closing_reason':'take_profit'}
    state = {'episode':observed, 'pending':{'tick':1, 'opening':False}}
    def fail(*_): raise ValueError('intentional fill failure')
    monkeypatch.setattr(executor.get_strategy('episode'), 'on_fill', fail)
    before = await snapshot(uid, pid)
    result = await executor.execute_bot_order(profile, decision('sell_foreign', '10', state), frame(pid))
    assert result.rejection == 'intentional fill failure' and result.fill is None
    assert await snapshot(uid, pid) == before
    async with async_session_maker() as db:
        p = await db.get(BotProfile, profile)
        assert p.strategy_state == {'episode':observed} and p.last_trade_at is None
        assert (await db.execute(select(func.count()).select_from(FxTrade))).scalar_one() == 0

@pytest.mark.asyncio
@pytest.mark.parametrize('side', ['buy', 'sell'])
async def test_human_http_and_bot_ordinary_spot_economics(side):
    profile, uid, pid, _ = await seed()
    async with async_session_maker() as db:
        human = User(username='human', cash=D('1000'), tos_accepted_at=datetime.now(timezone.utc))
        twin = FxPair(currency_code='TWIN', currency_name='Twin', status='trading', gold_reserve=D('10000'), foreign_reserve=D('10000'), buy_fee_rate=D('.01'), sell_fee_rate=D('.01'))
        db.add_all([human, twin])
        await db.flush()
        db.add_all([FxWallet(user_id=human.id, pair_id=twin.id, foreign_amount=D('100'), cost_basis=D('100')), FxTreasury(pair_id=twin.id, gold_balance=D('10000'), foreign_balance=D('10000'))])
        await db.commit()
        huid, hpid = human.id, twin.id
    app = FastAPI()
    app.include_router(fx.router)
    app.dependency_overrides[current_active_user] = lambda: human
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post(f'/pairs/{hpid}/trades', json={'side':side, 'amount':'1', 'min_out':'0', 'idempotency_key':'human'})
        assert response.status_code == 200, response.text
        result = await executor.execute_bot_order(profile, decision('buy_gold' if side == 'buy' else 'sell_foreign', '1'), frame(pid))
        assert result.rejection is None
        assert await snapshot(uid, pid) == await snapshot(huid, hpid)
        async with async_session_maker() as db: bot = await db.get(User, uid)
        app.dependency_overrides[current_active_user] = lambda: bot
        denied = await client.post(f'/pairs/{pid}/trades', json={'side':side, 'amount':'1', 'min_out':'0', 'idempotency_key':'public-bot'})
        assert denied.status_code == 403
    async with async_session_maker() as db:
        trades = (await db.execute(select(FxTrade).order_by(FxTrade.id))).scalars().all()
        economic = ('side', 'input_amount', 'output_amount', 'fee_amount', 'pre_gold_reserve', 'pre_foreign_reserve', 'post_gold_reserve', 'post_foreign_reserve', 'source', 'purpose')
        assert [getattr(trades[0], k) for k in economic] == [getattr(trades[1], k) for k in economic]
        events = (await db.execute(select(AuditEvent).where(AuditEvent.event_type == 'fx_trade').order_by(AuditEvent.id))).scalars().all()
        for k in ('side', 'input_amount', 'output_amount', 'fee_amount', 'pool_after', 'pool_version'):
            assert events[0].payload[k] == events[1].payload[k]

@pytest.mark.asyncio
async def test_partial_episode_exit_and_paused_admin_liquidation():
    profile, uid, pid, operator = await seed(template='episode')
    episode = {'direction':1, 'entry_tick':0, 'remaining':'100', 'basis':'100', 'realized_profit':'0', 'peak_return':.01, 'closing_reason':'stop_loss'}
    async with async_session_maker() as db:
        p = await db.get(BotProfile, profile)
        p.strategy_state = {'episode':episode}
        await db.commit()
    state = {'episode':episode, 'pending':{'tick':1, 'opening':False}}
    result = await executor.execute_bot_order(profile, decision('sell_foreign', '100', state), frame(pid))
    assert result.rejection is None and result.fill.input_amount == 50
    assert D(result.state['episode']['remaining']) == 50
    assert D(result.state['episode']['basis']) == 50
    assert result.state['episode']['closing_reason'] == 'stop_loss'
    denied = await executor.liquidate_fx_bot(profile, D('0'), operator)
    assert denied.rejection
    async with async_session_maker() as db:
        p = await db.get(BotProfile, profile)
        p.status = 'paused'
        await db.commit()
    # Old observations cannot overwrite paused state or resurrect the old size.
    await executor.save_observation_state(profile, FxDecision(state=state), frame(pid, 2))
    denied = await executor.liquidate_fx_bot(profile, D('0'), uid)
    assert denied.rejection
    closed = await executor.liquidate_fx_bot(profile, D('0'), operator)
    assert closed.rejection is None and closed.state['episode'] is None
    async with async_session_maker() as db:
        assert (await db.get(BotProfile, profile)).status == 'paused'

@pytest.mark.asyncio
@pytest.mark.parametrize('draw', ['0.123456789', '-0.123456789'])
async def test_real_random_strategy_intent_executes_at_storage_precision(draw):
    from app.services.pve_fx.types import FxBotSnapshot, FxExecutionView
    profile, uid, pid, _ = await seed()
    class Rng:
        def normal(self): return float(draw)
    strategy = executor.get_strategy('random')
    bot = FxBotSnapshot(profile, uid, pid, 'random', {'order_sigma_foreign':'1'},
                        D('1000'), D('100'))
    tick = frame(pid)
    intent = strategy.decide(bot, strategy.prepare_tick(bot, tick, Rng()),
                             FxExecutionView(D('10000'), D('10000'), D('.01'), D('.01')))
    result = await executor.execute_bot_order(profile, intent, tick)
    assert result.rejection is None
    assert result.fill.requested_amount == abs(D(draw))
    actual_foreign = (result.fill.output_amount if D(draw) > 0 else result.fill.input_amount)
    assert actual_foreign == D('0.123456')
