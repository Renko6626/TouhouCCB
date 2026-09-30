"""WP4a：外币义务扫描与未知 K 阻塞 run（无资金动作）。

只证明真实故障（spec §8.1–8.2）：

- `User.debt=0` 但持久外币本金/利息 > 0 的账户必须进入实际扫描候选，即使没有任何 run。
- 回补成本 K 无法完整报价（Q>=F）时不得拿 None 比门槛、不得当 0：
  必须建立/复用唯一 active run，并记录 blocked 动作与显式原因，不产生任何资金动作。
- 重复扫描不重复建 run、不重复记录 blocked 轮、不动现金/债务/储备/资产。

已知 E/B 的共享门槛（`triggered_basis`）用于扫描纳入：外币义务本身必须能让
低于共享维持门槛的账户被扫到，而不是因为金债为零被旧 D 门槛漏掉。
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.core.database import async_session_maker
from app.models.audit import AuditEvent
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
from app.models.fx import FxPair, FxShortPosition, FxTrade, FxTreasury, FxWallet
from app.services import liquidation_sweep, site_config
from app.services.credit import sweep
from app.services.credit.flags import CreditFlags, set_flags
from app.services.credit.gates import GATES
from app.services.credit.keys import GroupKey
from app.services.credit.ownership import OWNERSHIP
from app.services.credit.risk import discover_dependencies
from app.services.fx.quantize import amount_down
from app.services.fx.shorts import (
    ShortLiquidationRejected,
    ShortPostSettlementMismatch,
    ShortRetryCredit,
)

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


# ── WP4b: budgeted forced-cover transaction kernel ──────────────────────────
#
# 真实故障（spec §7.3/§8.1–8.2）：系统强制回补预算受限时若按计划量全买会
# 掏空现金、动用其他空头的锁金或借出金债；同 (run_id,round_no) 重试若二次
# 扣款会重复买币归还；预算/输出为零时若静默写 q=0 会制造免费回补。下面的
# 场景直接检查持久化钱币守恒、锁金/收益基准、审计包与幂等。

def _utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


async def _seed_cover(*, cash="1000", principal="100", interest="0",
                      restricted="100", basis="100", gold="1000", foreign="1000",
                      treasury_foreign="100000", treasury_gold=None, buy_fee="0",
                      status="trading", reduce_only=False, archived=False,
                      second_short=False, second_restricted="90",
                      debt="0", gold_age_sec=0, foreign_age_sec=0):
    """One funded pair with a real short row, optionally a second locked short.

    ``gold_age_sec`` / ``foreign_age_sec`` backdate the accrual clocks (with a
    positive ``debt`` / foreign tail) so a nonzero daily rate has real elapsed
    interest to settle.  ``treasury_gold`` defaults to the pair reserve but can
    be seeded independently to exercise the treasury storage bound.
    """
    async with async_session_maker() as s:
        now = datetime.now(timezone.utc)
        gold_clock = (
            now - timedelta(seconds=gold_age_sec) if D(debt) > 0 else None)
        user = User(username=uuid4().hex, casdoor_id=uuid4().hex,
                    cash=D(cash), debt=D(debt),
                    debt_last_accrued_at=gold_clock)
        s.add(user)
        await s.flush()
        pair = FxPair(
            currency_code=uuid4().hex[:16], currency_name="T",
            status=status, reduce_only=reduce_only, archived=archived,
            gold_reserve=D(gold), foreign_reserve=D(foreign),
            buy_fee_rate=D(buy_fee), sell_fee_rate=D("0"),
            short_lending_limit_foreign=D("100000"),
        )
        s.add(pair)
        await s.flush()
        s.add(FxTreasury(pair_id=pair.id,
                         gold_balance=D(gold if treasury_gold is None else treasury_gold),
                         foreign_balance=D(treasury_foreign)))
        total = D(principal) + D(interest)
        foreign_clock = (
            now - timedelta(seconds=foreign_age_sec) if total > 0 else None)
        s.add(FxShortPosition(
            user_id=user.id, pair_id=pair.id,
            principal_foreign=D(principal), interest_foreign=D(interest),
            interest_last_accrued_at=foreign_clock,
            restricted_gold=D(restricted), proceeds_basis_gold=D(basis),
        ))
        other_pid = None
        if second_short:
            other = FxPair(
                currency_code=uuid4().hex[:16], currency_name="T2",
                status="trading", gold_reserve=D("1000"), foreign_reserve=D("1000"),
                buy_fee_rate=D("0"), sell_fee_rate=D("0"),
                short_lending_limit_foreign=D("100000"),
            )
            s.add(other)
            await s.flush()
            s.add(FxShortPosition(
                user_id=user.id, pair_id=other.id,
                principal_foreign=D("10"), interest_foreign=D("0"),
                interest_last_accrued_at=datetime.now(timezone.utc),
                restricted_gold=D(second_restricted),
                proceeds_basis_gold=D(second_restricted),
            ))
            other_pid = other.id
        await s.commit()
        return int(user.id), int(pair.id), (None if other_pid is None else int(other_pid))


async def _liq_cover(uid, pid, *, run_id=1, round_no=1, planned="100",
                     budget="100", attempts=5):
    """Caller-owned transaction and full GATE set, exactly like WP4c will."""
    from app.services.fx.shorts import execute_liquidation_cover_in_session

    for _ in range(attempts):
        async with async_session_maker() as db:
            target = GroupKey("fx", pid)
            deps = await discover_dependencies(db, uid, extra_groups=[target])
            if db.in_transaction():
                await db.rollback()
            async with GATES.hold(
                exclusive=[target],
                shared=[g for g in deps.groups if g != target],
            ):
                try:
                    execution = await execute_liquidation_cover_in_session(
                        db, user_id=uid, pair_id=pid, run_id=run_id,
                        round_no=round_no, planned_amount=D(planned),
                        max_gold_budget=D(budget), credit_deps=deps,
                    )
                    await db.commit()
                    return execution
                except ShortRetryCredit:
                    await db.rollback()
                    continue
                except BaseException:
                    await db.rollback()
                    raise
    raise AssertionError("liquidation-cover retries exhausted")


async def _cover_state(db, uid, pid):
    user = await db.get(User, uid)
    pair = await db.get(FxPair, pid)
    treasury = (await db.execute(select(FxTreasury).where(
        FxTreasury.pair_id == pid))).scalars().one()
    shorts = (await db.execute(select(FxShortPosition).order_by(
        FxShortPosition.pair_id))).scalars().all()
    trades = (await db.execute(select(FxTrade).order_by(FxTrade.id))).scalars().all()
    audits = (await db.execute(select(AuditEvent).order_by(AuditEvent.id))).scalars().all()
    return {
        "cash": D(user.cash),
        "debt": D(user.debt),
        "pool_gold": D(pair.gold_reserve),
        "pool_foreign": D(pair.foreign_reserve),
        "pool_version": int(pair.pool_version),
        "treasury_gold": D(treasury.gold_balance),
        "treasury_foreign": D(treasury.foreign_balance),
        "shorts": [
            (int(s.pair_id), D(s.principal_foreign), D(s.interest_foreign),
             D(s.restricted_gold), D(s.proceeds_basis_gold))
            for s in shorts
        ],
        "trade_ids": [int(t.id) for t in trades],
        "trade_keys": [t.idempotency_key for t in trades],
        "audit_ids": [int(a.id) for a in audits],
    }


async def _wallet_sum(db):
    from sqlalchemy import func
    return D((await db.execute(select(func.coalesce(
        func.sum(FxWallet.foreign_amount), D("0"))))).scalar_one())


async def test_budget_limited_liquidation_cover_caps_planned_and_conserves():
    """预算 < exact-output 成本：按预算买正数量，不借金债，钱币守恒。"""
    uid, pid, _ = await _seed_cover(
        cash="1000", principal="100", restricted="100", basis="100",
        gold="1000", foreign="1000", treasury_foreign="100000", buy_fee="0")

    async with async_session_maker() as db:
        before = await _cover_state(db, uid, pid)
        wallet_before = await _wallet_sum(db)

    execution = await _liq_cover(uid, pid, run_id=7, round_no=1,
                                 planned="100", budget="100")

    assert execution.replay is False
    assert execution.blocked_reason is None
    assert execution.limited_by_cash is True
    assert execution.full_cover is False
    assert execution.paid_gold == D("100")
    assert execution.repaid_foreign == D("90.909090")
    assert execution.released_lock == D("90.909090")
    assert execution.principal_foreign_after == D("9.090910")
    assert execution.interest_foreign_after == D("0")
    assert execution.restricted_gold_after == D("9.090910")
    assert execution.proceeds_basis_gold_after == D("9.090910")

    async with async_session_maker() as db:
        after = await _cover_state(db, uid, pid)
        wallet_after = await _wallet_sum(db)
        trade = (await db.execute(select(FxTrade).where(
            FxTrade.id == execution.trade_id))).scalars().one()
        audit = (await db.execute(select(AuditEvent).where(
            AuditEvent.event_type == "fx_trade",
            AuditEvent.ref_id == trade.id))).scalars().one()
        payload = audit.payload

        # Real economic legs: cash down by the budget, pool gold up by net,
        # treasury takes the gold fee and the exactly repaid q, no wallet.
        assert D(trade.input_amount) == D("100")
        assert D(trade.output_amount) == D("90.909090")
        assert trade.purpose == "short_cover" and trade.side == "buy"
        assert trade.source == "liquidation"
        assert D(trade.requested_foreign_amount) == D("100")
        assert D(trade.max_gold_in) == D("100")
        assert trade.cover_all is None

        assert after["cash"] == before["cash"] - D("100")
        assert after["pool_gold"] == before["pool_gold"] + D("100")
        assert after["pool_foreign"] == before["pool_foreign"] - D("90.909090")
        assert after["treasury_foreign"] == before["treasury_foreign"] + D("90.909090")
        assert after["treasury_gold"] == before["treasury_gold"]
        assert after["pool_version"] == before["pool_version"] + 1
        assert after["debt"] == D("0")  # forced cover never originates gold debt
        assert wallet_after == wallet_before == D("0")

        target = next(s for s in after["shorts"] if s[0] == pid)
        assert target[1] == D("9.090910")
        assert target[2] == D("0")
        assert target[3] == D("9.090910")
        assert target[4] == D("9.090910")

        # Conservation: pool + treasury + wallets is constant for both currencies.
        assert (after["pool_gold"] + after["treasury_gold"] + after["cash"]
                == before["pool_gold"] + before["treasury_gold"] + before["cash"])
        assert (after["pool_foreign"] + after["treasury_foreign"] + wallet_after
                == before["pool_foreign"] + before["treasury_foreign"] + wallet_before)

        # Replayable audit package: purpose/legs and the forced-source flag.
        assert payload["source"] == "liquidation"
        assert payload["purpose"] == "short_cover"
        assert payload["fee_currency"] == "gold"
        assert payload["limited_by_cash"] is True
        assert D(payload["repaid_foreign"]) == D("90.909090")
        assert D(payload["paid_gold"]) == D("100")
        assert D(payload["principal_paid_foreign"]) == D("90.909090")
        assert D(payload["interest_paid_foreign"]) == D("0")
        assert D(payload["released_lock"]) == D("90.909090")
        assert D(payload["allocated_proceeds_basis"]) == D("90.909090")
        assert D(payload["realized_pl"]) == D("-9.090910")


async def test_liquidation_cover_replays_same_run_round_without_second_move():
    """同 (run_id,round_no) 重试返回原成交，二次不动钱/币/债；改参冲突。"""
    uid, pid, _ = await _seed_cover(
        cash="1000", principal="100", restricted="100", basis="100",
        gold="1000", foreign="1000", treasury_foreign="100000", buy_fee="0")

    first = await _liq_cover(uid, pid, run_id=11, round_no=3,
                             planned="50", budget="1000000")
    assert first.replay is False
    assert first.limited_by_cash is False
    assert first.repaid_foreign == D("50")
    assert first.paid_gold == D("52.631579")

    async with async_session_maker() as db:
        before_replay = await _cover_state(db, uid, pid)

    replay = await _liq_cover(uid, pid, run_id=11, round_no=3,
                              planned="50", budget="1000000")
    assert replay.replay is True
    assert replay.trade_id == first.trade_id

    async with async_session_maker() as db:
        after_replay = await _cover_state(db, uid, pid)
    assert after_replay == before_replay
    assert after_replay["trade_ids"] == before_replay["trade_ids"]

    # Same key, changed planned/budget identity: conflict, no new trade.
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await _liq_cover(uid, pid, run_id=11, round_no=3,
                         planned="60", budget="1000000")
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc2:
        await _liq_cover(uid, pid, run_id=11, round_no=3,
                         planned="50", budget="999999")
    assert exc2.value.status_code == 409

    async with async_session_maker() as db:
        after_conflict = await _cover_state(db, uid, pid)
    assert after_conflict == before_replay


async def test_liquidation_cover_zero_budget_and_zero_output_block_untouched():
    """预算为零或输出向下归零：blocked 返回，不动钱/币/债/成交/审计。"""
    uid, pid, _ = await _seed_cover(
        cash="1000", principal="100", restricted="100", basis="100",
        gold="1000", foreign="200", treasury_foreign="100000", buy_fee="0")

    async with async_session_maker() as db:
        before = await _cover_state(db, uid, pid)

    zero_budget = await _liq_cover(uid, pid, run_id=21, round_no=1,
                                   planned="100", budget="0")
    assert zero_budget.trade is None
    assert zero_budget.replay is False
    assert zero_budget.blocked_reason is not None

    async with async_session_maker() as db:
        after_zero_budget = await _cover_state(db, uid, pid)
    assert after_zero_budget == before

    zero_output = await _liq_cover(uid, pid, run_id=21, round_no=2,
                                   planned="100", budget="0.000001")
    assert zero_output.trade is None
    assert zero_output.replay is False
    assert zero_output.blocked_reason is not None

    async with async_session_maker() as db:
        after_zero_output = await _cover_state(db, uid, pid)
    assert after_zero_output == before


async def test_liquidation_cover_uses_only_own_lock_and_free_cash():
    """预算 = min(上限, 未锁现金 + 本仓锁金)；不得动另一空头的锁金。"""
    uid, pid, other_pid = await _seed_cover(
        cash="120", principal="10", restricted="20", basis="5",
        gold="1000", foreign="205", treasury_foreign="100000", buy_fee="0",
        second_short=True, second_restricted="90")

    execution = await _liq_cover(uid, pid, run_id=31, round_no=1,
                                 planned="10", budget="1000000")
    assert execution.blocked_reason is None
    assert execution.limited_by_cash is True
    # budget = free cash (120-110=10) + own lock (20) = 30, not 120.
    assert execution.paid_gold == D("30")
    assert execution.repaid_foreign == D("5.970873")
    assert execution.released_lock == D("20")
    assert execution.restricted_gold_after == D("0")

    async with async_session_maker() as db:
        user = await db.get(User, uid)
        target = (await db.execute(select(FxShortPosition).where(
            FxShortPosition.pair_id == pid))).scalars().one()
        other = (await db.execute(select(FxShortPosition).where(
            FxShortPosition.pair_id == other_pid))).scalars().one()
        assert D(user.cash) == D("90")
        assert D(target.restricted_gold) == D("0")
        assert D(target.principal_foreign) == D("4.029127")
        assert D(target.proceeds_basis_gold) == D("2.014564")
        # The other short keeps every unit of its own lock and basis.
        assert D(other.restricted_gold) == D("90")
        assert D(other.proceeds_basis_gold) == D("90")


async def test_liquidation_cover_full_repayment_clears_both_tails():
    """预算足够时按 exact-output 买足整仓，锁金/收益基准/计息时点全部结清。"""
    uid, pid, _ = await _seed_cover(
        cash="1000", principal="100", restricted="100", basis="100",
        gold="1000", foreign="1000", treasury_foreign="100000", buy_fee="0")

    execution = await _liq_cover(uid, pid, run_id=41, round_no=1,
                                 planned="100", budget="1000000")
    assert execution.blocked_reason is None
    assert execution.full_cover is True
    assert execution.limited_by_cash is False
    assert execution.repaid_foreign == D("100")
    assert execution.paid_gold == D("111.111112")
    assert execution.released_lock == D("100")
    assert execution.principal_foreign_after == D("0")
    assert execution.interest_foreign_after == D("0")
    assert execution.restricted_gold_after == D("0")
    assert execution.proceeds_basis_gold_after == D("0")
    assert execution.accrued_at is not None

    async with async_session_maker() as db:
        target = (await db.execute(select(FxShortPosition).where(
            FxShortPosition.pair_id == pid))).scalars().one()
        trade = (await db.execute(select(FxTrade).where(
            FxTrade.id == execution.trade_id))).scalars().one()
        audit = (await db.execute(select(AuditEvent).where(
            AuditEvent.event_type == "fx_trade",
            AuditEvent.ref_id == trade.id))).scalars().one()
        assert D(target.principal_foreign) == D("0")
        assert D(target.interest_foreign) == D("0")
        assert D(target.restricted_gold) == D("0")
        assert D(target.proceeds_basis_gold) == D("0")
        assert target.interest_last_accrued_at is None
        assert audit.payload["full_cover"] is True
        assert audit.payload["limited_by_cash"] is False
        assert D(audit.payload["realized_pl"]) == D("-11.111112")


async def test_liquidation_cover_q_ge_f_blocks_without_spending_cash():
    """Q>=F 无法完整买回：记录阻塞，该组不自动消耗现金（spec §8.2）。"""
    uid, pid, _ = await _seed_cover(
        cash="1000", principal="1000", restricted="0", basis="0",
        gold="1000", foreign="1000", treasury_foreign="100000", buy_fee="0")

    async with async_session_maker() as db:
        before = await _cover_state(db, uid, pid)

    execution = await _liq_cover(uid, pid, run_id=51, round_no=1,
                                 planned="1000", budget="1000000")
    assert execution.trade is None
    assert execution.blocked_reason == "insufficient_pool_foreign"

    async with async_session_maker() as db:
        after = await _cover_state(db, uid, pid)
    assert after == before


async def test_liquidation_cover_requires_unified_credit_fail_closed():
    """统一信贷关闭时强平回补 403，不遗留任何钱币动作。"""
    from fastapi import HTTPException

    uid, pid, _ = await _seed_cover(cash="1000", principal="100", restricted="100",
                                    basis="100", gold="1000", foreign="1000")
    async with async_session_maker() as db:
        before = await _cover_state(db, uid, pid)

    set_flags(CreditFlags(
        unified_credit_enabled=False, credit_leverage=D("20"),
        credit_maintenance_ratio=D(".04")))

    with pytest.raises(HTTPException) as exc:
        await _liq_cover(uid, pid, run_id=61, round_no=1,
                         planned="100", budget="1000000")
    assert exc.value.status_code == 403

    async with async_session_maker() as db:
        after = await _cover_state(db, uid, pid)
    assert after == before


async def _blocked_round_snapshot(db, uid, pid):
    """Every economic field a blocked forced round must leave byte-identical."""
    user = await db.get(User, uid)
    pair = await db.get(FxPair, pid)
    target = (await db.execute(select(FxShortPosition).where(
        FxShortPosition.pair_id == pid))).scalars().one()
    treasury = (await db.execute(select(FxTreasury).where(
        FxTreasury.pair_id == pid))).scalars().one()
    audit_count = int((await db.execute(
        select(func.count()).select_from(AuditEvent))).scalar_one())
    return {
        "gold_debt": D(user.debt),
        "gold_debt_clock": user.debt_last_accrued_at,
        "economic_version": int(user.economic_version),
        "cash": D(user.cash),
        "foreign_principal": D(target.principal_foreign),
        "foreign_interest": D(target.interest_foreign),
        "foreign_clock": target.interest_last_accrued_at,
        "pool_gold": D(pair.gold_reserve),
        "pool_foreign": D(pair.foreign_reserve),
        "pool_version": int(pair.pool_version),
        "treasury_gold": D(treasury.gold_balance),
        "treasury_foreign": D(treasury.foreign_balance),
        "audit_count": audit_count,
    }


async def test_blocked_liquidation_cover_commits_with_zero_interest_and_audit_mutation():
    """nonzero rate + elapsed interest + Q>=F: a committed blocked round is inert.

    Real fault (spec §7.3/§8.2): the kernel used to run the one-T interest
    settlement *before* the Q>=F / reserve / quote blocked checks.  WP4c may
    persist a blocked round and commit it, so that ordering silently advanced
    the gold/foreign debt clocks and wrote interest audit events with no cover,
    no economic-version bump and no trade.  This seeds 1%/day with 30 elapsed
    days on both the gold debt and the foreign short (so the would-be settlement
    is clearly nonzero), drives the canonical Q>=F block, commits the round like
    WP4c and requires every economic field to stay byte-identical.
    """
    async with async_session_maker() as s:
        cfg = (await s.execute(select(SiteConfig).where(
            SiteConfig.key == 'loan_daily_rate'))).scalar_one()
        cfg.value = '0.01'
        await s.commit()
    site_config.clear_cache()

    uid, pid, _ = await _seed_cover(
        cash="1000", principal="1000", interest="0", restricted="0", basis="0",
        gold="1000", foreign="1000", treasury_foreign="100000", buy_fee="0",
        debt="100", gold_age_sec=30 * 86400, foreign_age_sec=30 * 86400)

    async with async_session_maker() as db:
        before = await _blocked_round_snapshot(db, uid, pid)
    assert before["gold_debt"] == D("100")
    assert before["foreign_interest"] == D("0")

    execution = await _liq_cover(uid, pid, run_id=71, round_no=1,
                                 planned="1000", budget="1000000")
    assert execution.trade is None
    assert execution.replay is False
    assert execution.blocked_reason == "insufficient_pool_foreign"

    async with async_session_maker() as db:
        after = await _blocked_round_snapshot(db, uid, pid)
    assert after == before


#: ``FxTreasury.gold_balance`` is ``Numeric(16,6)``; this is its largest value.
_TREASURY_GOLD_MAX = D("9999999999.999999")


async def test_post_settlement_bound_failure_requires_whole_transaction_rollback():
    """A forced cover whose *post*-settlement bound fails must roll back, not block.

    Real fault (spec §7.3/§8.2, review I1): ``_write_cover_ledger`` runs its
    storage-bound checks after ``_settle_interest_at_T`` already advanced the
    gold/foreign interest clocks and wrote their audit rows.  Raising the
    pre-mutation ``ShortLiquidationRejected`` there lets a WP4c caller mistake
    the failure for a normal blocked round and commit interest with no trade and
    no economic-version bump.  This seeds 1%/day with 30 elapsed days plus a
    treasury gold balance at the ``Numeric(16,6)`` maximum (so
    ``treasury_gold + fee`` overflows only after settlement), drives the kernel
    directly, and requires:

    - the distinct rollback-required :class:`ShortPostSettlementMismatch`
      (never the pre-mutation :class:`ShortLiquidationRejected`), and
    - after catching it and rolling the whole transaction back, every clock,
      debt, cash, pool, treasury, short row, trade and audit row byte-identical.
    """
    async with async_session_maker() as s:
        cfg = (await s.execute(select(SiteConfig).where(
            SiteConfig.key == 'loan_daily_rate'))).scalar_one()
        cfg.value = '0.01'
        await s.commit()
    site_config.clear_cache()

    uid, pid, _ = await _seed_cover(
        cash="1000", principal="100", interest="0", restricted="100", basis="100",
        gold="1000", foreign="1000", treasury_foreign="100000", buy_fee="0.01",
        treasury_gold=str(_TREASURY_GOLD_MAX), debt="100",
        gold_age_sec=30 * 86400, foreign_age_sec=30 * 86400)

    async with async_session_maker() as db:
        before_econ = await _blocked_round_snapshot(db, uid, pid)
        before_state = await _cover_state(db, uid, pid)
    assert before_econ["gold_debt"] == D("100")
    assert before_econ["foreign_interest"] == D("0")
    assert before_econ["treasury_gold"] > D("9999999999")

    from app.services.fx.shorts import execute_liquidation_cover_in_session

    async with async_session_maker() as db:
        target = GroupKey("fx", pid)
        deps = await discover_dependencies(db, uid, extra_groups=[target])
        if db.in_transaction():
            await db.rollback()
        async with GATES.hold(
            exclusive=[target],
            shared=[g for g in deps.groups if g != target],
        ):
            with pytest.raises(ShortPostSettlementMismatch) as excinfo:
                await execute_liquidation_cover_in_session(
                    db, user_id=uid, pair_id=pid, run_id=81, round_no=1,
                    planned_amount=D("100"), max_gold_budget=D("1000000"),
                    credit_deps=deps,
                )
            # Distinct from every pre-mutation rejection: a caller that recorded
            # this as a committable blocked round would be wrong by type.
            assert not isinstance(excinfo.value, ShortLiquidationRejected)
            # The settlement already moved the clocks and wrote audits inside this
            # transaction, so a commit would persist interest with no trade.
            dirty_user = await db.get(User, uid)
            dirty_target = (await db.execute(select(FxShortPosition).where(
                FxShortPosition.pair_id == pid))).scalars().one()
            dirty_audit_count = int((await db.execute(
                select(func.count()).select_from(AuditEvent))).scalar_one())
            assert D(dirty_user.debt) > before_econ["gold_debt"]
            assert D(dirty_target.interest_foreign) > before_econ["foreign_interest"]
            assert dirty_audit_count > before_econ["audit_count"]
            # WP4c's contract: roll the whole transaction back, never commit the
            # blocked-looking round.
            await db.rollback()

    async with async_session_maker() as db:
        after_econ = await _blocked_round_snapshot(db, uid, pid)
        after_state = await _cover_state(db, uid, pid)
    assert after_econ == before_econ
    assert after_state == before_state
