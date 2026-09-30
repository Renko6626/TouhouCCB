"""WP4a：外币义务扫描与未知 K 阻塞 run（无资金动作）。

只证明真实故障（spec §8.1–8.2）：

- `User.debt=0` 但持久外币本金/利息 > 0 的账户必须进入实际扫描候选，即使没有任何 run。
- 回补成本 K 无法完整报价（Q>=F）时不得拿 None 比门槛、不得当 0：
  必须建立/复用唯一 active run，并记录 blocked 动作与显式原因，不产生任何资金动作。
- 重复扫描不重复建 run、不重复记录 blocked 轮、不动现金/债务/储备/资产。

已知 E/B 的共享门槛（`triggered_basis`）用于扫描纳入：外币义务本身必须能让
低于共享维持门槛的账户被扫到，而不是因为金债为零被旧 D 门槛漏掉。
"""
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.base import (
    LiquidationEvent,
    Market,
    MarketStatus,
    Outcome,
    Position,
    SiteConfig,
    User,
)
from app.models.credit import LiquidationAction, LiquidationRun
from app.models.fx import FxPair, FxShortPosition, FxTreasury
from app.services import liquidation_sweep, site_config
from app.services.credit import sweep
from app.services.credit.flags import CreditFlags, set_flags
from app.services.credit.ownership import OWNERSHIP

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def lifecycle():
    await OWNERSHIP.acquire()
    set_flags(CreditFlags(
        unified_credit_enabled=True,
        credit_leverage=D('20'),
        credit_maintenance_ratio=D('.04'),
    ))
    async with async_session_maker() as s:
        for key, value in (
            ('liquidation_enabled', 'true'),
            ('loan_daily_rate', '0'),
            ('liquidation_partial_pct', '.1'),
        ):
            cfg = (await s.execute(
                select(SiteConfig).where(SiteConfig.key == key)
            )).scalar_one_or_none()
            if cfg is None:
                s.add(SiteConfig(key=key, value=value, value_type='str'))
            else:
                cfg.value = value
        await s.commit()
    site_config.clear_cache()
    yield
    set_flags(CreditFlags())


async def _seed_foreign_only_overflow(*, principal='1000', foreign_reserve='1000',
                                      cash='50', with_asset=False):
    """Q>=F 外币义务账户；可选一个可执行的 LMSR 多头证明未知 K 不卖资产。"""
    now = datetime.now(timezone.utc)
    async with async_session_maker() as s:
        user = User(username='fx_overflow', casdoor_id='fx_overflow',
                    cash=D(cash), debt=D('0'))
        s.add(user)
        await s.flush()
        pair = FxPair(
            currency_code='OVF', currency_name='Overflow',
            status='trading',
            gold_reserve=D('1000'), foreign_reserve=D(foreign_reserve),
            buy_fee_rate=D('0'), sell_fee_rate=D('0'),
        )
        s.add(pair)
        await s.flush()
        s.add(FxTreasury(pair_id=pair.id, gold_balance=D('1000'),
                         foreign_balance=D(foreign_reserve)))
        s.add(FxShortPosition(
            user_id=user.id, pair_id=pair.id,
            principal_foreign=D(principal), interest_foreign=D('0'),
            interest_last_accrued_at=now,
            restricted_gold=D('0'), proceeds_basis_gold=D('0'),
        ))
        if with_asset:
            market = Market(title='fx_liq_asset', liquidity_b=100,
                            status=MarketStatus.TRADING)
            s.add(market)
            await s.flush()
            a = Outcome(market_id=market.id, label='a', total_shares=D('100'))
            b = Outcome(market_id=market.id, label='b', total_shares=D('0'))
            s.add_all([a, b])
            await s.flush()
            s.add(Position(user_id=user.id, outcome_id=a.id, amount=D('10'),
                           cost_basis=D('0')))
        await s.commit()
        return user.id, pair.id


async def test_foreign_only_overflow_sweep_creates_one_blocked_run_without_money():
    uid, pair_id = await _seed_foreign_only_overflow(with_asset=True)

    first = await liquidation_sweep.run_liquidation_sweep_once()
    assert first.get('blocked_count') == 1
    assert first.get('monetary_action_count') == 0

    async with async_session_maker() as s:
        runs = list((await s.execute(select(LiquidationRun))).scalars())
        assert len(runs) == 1
        run = runs[0]
        assert run.status == 'active'
        assert run.pre_liquidation_equity is None
        assert run.pre_risk_basis is None

        actions = list((await s.execute(select(LiquidationAction))).scalars())
        assert len(actions) == 1
        action = actions[0]
        assert action.kind == 'blocked'
        assert action.blocked_reason == 'insufficient_pool_foreign'
        assert action.product == 'fx'
        assert action.group_id == pair_id
        assert action.repaid == 0
        assert not list((await s.execute(select(LiquidationEvent))).scalars())

        # 资金/债务/储备/资产全部不动。
        user = await s.get(User, uid)
        assert user.cash == D('50') and user.debt == D('0')
        short = (await s.execute(select(FxShortPosition))).scalar_one()
        assert short.principal_foreign == D('1000')
        assert short.interest_foreign == D('0')
        treasury = (await s.execute(select(FxTreasury))).scalar_one()
        assert treasury.gold_balance == D('1000')
        assert treasury.foreign_balance == D('1000')
        position = (await s.execute(select(Position))).scalar_one()
        assert position.amount == D('10')

    # 重复扫描不重复建 run、不重复记录 blocked 轮、不产生资金动作。
    for _ in range(2):
        repeated = await liquidation_sweep.run_liquidation_sweep_once()
        assert repeated.get('monetary_action_count') == 0

    async with async_session_maker() as s:
        runs = list((await s.execute(select(LiquidationRun))).scalars())
        assert len(runs) == 1 and runs[0].status == 'active'
        actions = list((await s.execute(select(LiquidationAction))).scalars())
        assert len(actions) == 1 and actions[0].kind == 'blocked'
        user = await s.get(User, uid)
        assert user.cash == D('50') and user.debt == D('0')
        treasury = (await s.execute(select(FxTreasury))).scalar_one()
        assert treasury.gold_balance == D('1000')
        assert treasury.foreign_balance == D('1000')
        assert not list((await s.execute(select(LiquidationEvent))).scalars())


async def test_foreign_short_included_by_shared_basis_even_with_zero_gold_debt(monkeypatch):
    """D=0 的空头账户低于共享维持门槛时必须被扫到（旧 D 门槛会漏）。"""
    now = datetime.now(timezone.utc)
    async with async_session_maker() as s:
        user = User(username='fx_shared', casdoor_id='fx_shared',
                    cash=D('0'), debt=D('0'))
        s.add(user)
        await s.flush()
        pair = FxPair(
            currency_code='SHR', currency_name='Shared',
            status='trading',
            gold_reserve=D('10000'), foreign_reserve=D('50000'),
            buy_fee_rate=D('0'), sell_fee_rate=D('0'),
        )
        s.add(pair)
        await s.flush()
        s.add(FxShortPosition(
            user_id=user.id, pair_id=pair.id,
            principal_foreign=D('500'), interest_foreign=D('0'),
            interest_last_accrued_at=now,
            restricted_gold=D('0'), proceeds_basis_gold=D('0'),
        ))
        await s.commit()
        uid = user.id

    seen = []

    async def execute(uid_arg, **kwargs):
        seen.append(uid_arg)
        return 'blocked'

    monkeypatch.setattr(sweep, 'execute_user', execute)
    result = await liquidation_sweep.run_liquidation_sweep_once()
    assert uid in seen
    assert result['scanned_count'] >= 1


async def _seed_healthy_and_overflow_shorts(*, cash='50'):
    """同一账户两个空头对：一个可完整报价（健康），一个 Q>=F（未知 K）。"""
    now = datetime.now(timezone.utc)
    async with async_session_maker() as s:
        user = User(username='fx_multi', casdoor_id='fx_multi',
                    cash=D(cash), debt=D('0'))
        s.add(user)
        await s.flush()
        healthy = FxPair(
            currency_code='HLT', currency_name='Healthy',
            status='trading',
            gold_reserve=D('100000'), foreign_reserve=D('100000'),
            buy_fee_rate=D('0'), sell_fee_rate=D('0'),
        )
        overflow = FxPair(
            currency_code='OVR', currency_name='Overflow',
            status='trading',
            gold_reserve=D('1000'), foreign_reserve=D('1000'),
            buy_fee_rate=D('0'), sell_fee_rate=D('0'),
        )
        s.add_all([healthy, overflow])
        await s.flush()
        s.add_all([
            FxTreasury(pair_id=healthy.id, gold_balance=D('100000'),
                       foreign_balance=D('100000')),
            FxTreasury(pair_id=overflow.id, gold_balance=D('1000'),
                       foreign_balance=D('1000')),
            FxShortPosition(
                user_id=user.id, pair_id=healthy.id,
                principal_foreign=D('100'), interest_foreign=D('0'),
                interest_last_accrued_at=now,
                restricted_gold=D('0'), proceeds_basis_gold=D('0'),
            ),
            FxShortPosition(
                user_id=user.id, pair_id=overflow.id,
                principal_foreign=D('1000'), interest_foreign=D('0'),
                interest_last_accrued_at=now,
                restricted_gold=D('0'), proceeds_basis_gold=D('0'),
            ),
        ])
        await s.commit()
        return user.id, healthy.id, overflow.id


async def test_multi_pair_unknown_k_blocked_action_names_the_blocked_pair_once():
    """健康空头排在最前，也不能把 Q>=F 那对的阻塞原因记到健康 pair 上。

    真实故障：`_short_group` 取排序后第一个空头，而排序按 K 降序、未知 K 排最后，
    于是「健康且高 K」的 pair 会被写进 `insufficient_pool_foreign` 动作的 group_id，
    审计/运维误判；K 漂移换 pair 还会重复记 blocked 轮。这里断言 durable 动作绑定
    真正阻塞的 Q>=F pair，且重复实际扫描只有一轮、无资金动作。
    """
    uid, healthy_id, overflow_id = await _seed_healthy_and_overflow_shorts()

    first = await liquidation_sweep.run_liquidation_sweep_once()
    assert first.get('blocked_count') == 1
    assert first.get('monetary_action_count') == 0

    async with async_session_maker() as s:
        runs = list((await s.execute(select(LiquidationRun))).scalars())
        assert len(runs) == 1
        run = runs[0]
        assert run.status == 'active'
        assert run.pre_liquidation_equity is None
        assert run.pre_risk_basis is None

        actions = list((await s.execute(select(LiquidationAction))).scalars())
        assert len(actions) == 1
        action = actions[0]
        assert action.kind == 'blocked'
        assert action.blocked_reason == 'insufficient_pool_foreign'
        assert action.product == 'fx'
        assert action.group_id == overflow_id
        assert action.group_id != healthy_id
        assert action.round_no == 1
        assert action.repaid == 0
        assert not list((await s.execute(select(LiquidationEvent))).scalars())

        user = await s.get(User, uid)
        assert user.cash == D('50') and user.debt == D('0')
        shorts = {s_.pair_id: s_ for s_ in
                  (await s.execute(select(FxShortPosition))).scalars()}
        assert shorts[healthy_id].principal_foreign == D('100')
        assert shorts[overflow_id].principal_foreign == D('1000')
        treasuries = {t.pair_id: t for t in
                      (await s.execute(select(FxTreasury))).scalars()}
        assert treasuries[healthy_id].gold_balance == D('100000')
        assert treasuries[healthy_id].foreign_balance == D('100000')
        assert treasuries[overflow_id].gold_balance == D('1000')
        assert treasuries[overflow_id].foreign_balance == D('1000')

    # 第二次实际扫描：同一阻塞状态不新增 run/轮，pair 归属不变，资金仍不动。
    second = await liquidation_sweep.run_liquidation_sweep_once()
    assert second.get('blocked_count') == 1
    assert second.get('monetary_action_count') == 0

    async with async_session_maker() as s:
        runs = list((await s.execute(select(LiquidationRun))).scalars())
        assert len(runs) == 1 and runs[0].status == 'active'
        actions = list((await s.execute(select(LiquidationAction))).scalars())
        assert len(actions) == 1
        assert actions[0].group_id == overflow_id
        assert actions[0].blocked_reason == 'insufficient_pool_foreign'
        user = await s.get(User, uid)
        assert user.cash == D('50') and user.debt == D('0')
        assert not list((await s.execute(select(LiquidationEvent))).scalars())
