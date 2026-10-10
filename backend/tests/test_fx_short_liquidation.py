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
from app.services.credit.flags import CreditFlags, set_flags
from app.services.credit.gates import GATES
from app.services.credit.keys import GroupKey
from app.services.credit.ownership import OWNERSHIP
from app.services.credit.risk import discover_dependencies
from app.services.fx.shorts import (
    ShortLiquidationRejected,
    ShortPostSettlementMismatch,
    ShortRejected,
    ShortRetryCredit,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def lifecycle():
    await OWNERSHIP.acquire()
    set_flags(CreditFlags(
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
    uid, pid, _ = await _seed_cover(
        cash=cash, principal=principal, restricted="0", basis="0",
        foreign=foreign_reserve, treasury_foreign=foreign_reserve,
    )
    if with_asset:
        async with async_session_maker() as s:
            market = Market(title='fx_liq_asset', liquidity_b=100,
                            status=MarketStatus.TRADING)
            s.add(market)
            await s.flush()
            a = Outcome(market_id=market.id, label='a', total_shares=D('100'))
            b = Outcome(market_id=market.id, label='b', total_shares=D('0'))
            s.add_all([a, b])
            await s.flush()
            s.add(Position(user_id=uid, outcome_id=a.id, amount=D('10'), cost_basis=D('0')))
            await s.commit()
    return uid, pid


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

async def _seed_cover(*, cash="1000", principal="100", interest="0",
                      restricted="100", basis="100", gold="1000", foreign="1000",
                      treasury_foreign="100000", treasury_gold=None, buy_fee="0",
                      status="trading", reduce_only=False, archived=False,
                      second_short=False, second_restricted="90",
                      second_principal="10", second_interest="0",
                      second_age_sec=0,
                      debt="0", gold_age_sec=0, foreign_age_sec=0):
    """One funded pair with a real short row, optionally a second locked short.

    ``gold_age_sec`` / ``foreign_age_sec`` backdate the accrual clocks (with a
    positive ``debt`` / foreign tail) so a nonzero daily rate has real elapsed
    interest to settle.  ``treasury_gold`` defaults to the pair reserve but can
    be seeded independently to exercise the treasury storage bound.
    ``second_principal`` / ``second_interest`` / ``second_age_sec`` size the
    optional sibling short; its pair is created after the target, so
    ``pair_id`` order settles the target first and can leave a sibling overflow
    for T.
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
            sibling_total = D(second_principal) + D(second_interest)
            sibling_clock = (
                now - timedelta(seconds=second_age_sec) if sibling_total > 0 else None)
            s.add(FxShortPosition(
                user_id=user.id, pair_id=other.id,
                principal_foreign=D(second_principal),
                interest_foreign=D(second_interest),
                interest_last_accrued_at=sibling_clock,
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


#: ``principal_foreign`` is ``Numeric(24,6)``; its largest integral principal,
#: which a nonzero rate then pushes past the foreign storage bound at T.
_FOREIGN_DEBT_MAX = D("999999999999999999")


async def test_sibling_settlement_overflow_requires_whole_transaction_rollback():
    """A sibling short's T overflow must roll back, never look like a block.

    Real fault (spec §7.3/§8.2, WP4b-fix1 review Minor):
    ``_settle_interest_at_T`` settles *every* own short row, but only the target
    quantity is preflighted.  A sibling pair whose pending debt overflows
    ``Numeric(24,6)`` raises the base :class:`ShortRejected` from
    ``pending_short_debt`` *during* settlement — after the gold debt and the
    earlier (lower ``pair_id``) target short already accrued and wrote interest
    audit rows.  Left as a plain ``ShortRejected`` it is indistinguishable from a
    pre-mutation rejection, so a WP4c caller could commit a partial settlement
    with no trade and no economic-version bump.  This seeds a valid target cover
    plus a sibling at the foreign storage maximum and drives the kernel directly:
    the failure must be the rollback-required
    :class:`ShortPostSettlementMismatch` (never the pre-mutation
    :class:`ShortLiquidationRejected`), preserve the original sibling
    ``ShortRejected`` as its cause, and, after the caller rolls the whole
    transaction back, leave every debt/clock/version/audit/pool/treasury/trade row
    byte-identical.
    """
    async with async_session_maker() as s:
        cfg = (await s.execute(select(SiteConfig).where(
            SiteConfig.key == 'loan_daily_rate'))).scalar_one()
        cfg.value = '0.01'
        await s.commit()
    site_config.clear_cache()

    uid, pid, other_pid = await _seed_cover(
        cash="1000", principal="100", interest="0", restricted="100", basis="100",
        gold="1000", foreign="1000", treasury_foreign="100000", buy_fee="0",
        debt="100", gold_age_sec=30 * 86400, foreign_age_sec=30 * 86400,
        second_short=True, second_restricted="90",
        second_principal=str(_FOREIGN_DEBT_MAX), second_age_sec=30 * 86400)
    assert other_pid is not None

    async with async_session_maker() as db:
        before_econ = await _blocked_round_snapshot(db, uid, pid)
        before_state = await _cover_state(db, uid, pid)
    assert before_econ["gold_debt"] == D("100")
    assert before_econ["foreign_interest"] == D("0")

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
                    db, user_id=uid, pair_id=pid, run_id=91, round_no=1,
                    planned_amount=D("100"), max_gold_budget=D("1000000"),
                    credit_deps=deps,
                )
            # A post-settlement sibling overflow is never the pre-mutation type.
            assert not isinstance(excinfo.value, ShortLiquidationRejected)
            assert isinstance(excinfo.value, ShortRetryCredit)
            # The original settlement failure is preserved as the cause.
            assert isinstance(excinfo.value.__cause__, ShortRejected)
            # Settlement already moved the gold debt, the target short and audit
            # inside this transaction, so a commit would persist interest with no
            # trade and no economic-version bump.
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


# ── WP4c: known-E ordering, cash retention and one-group execution ───────────
#
# 真实故障（spec §8.1）：最大空头当前买不起时若直接还金债/判恢复，会把卖资产
# 所得立刻还债，下一轮空头仍无预算；或对金债为零的空头账户走 `debt==0` 快路径
# 伪恢复，永久不再处置外币义务；或同一 (run_id,round_no) 重复扣款。

async def _seed_rotation(*, cash="0", debt="50", short_principal="10000",
                         short_gold="10000", short_foreign="100000",
                         asset_foreign="500", asset_gold="1000",
                         asset_reserve="10000"):
    """零未锁现金的最大空头 + 两组正 FX 资产 + 正金债（rotation 场景）。"""
    now = datetime.now(timezone.utc)
    async with async_session_maker() as s:
        user = User(username=uuid4().hex, casdoor_id=uuid4().hex,
                    cash=D(cash), debt=D(debt),
                    debt_last_accrued_at=now if D(debt) > 0 else None)
        s.add(user)
        await s.flush()
        short_pair = FxPair(
            currency_code=uuid4().hex[:16], currency_name="S", status="trading",
            gold_reserve=D(short_gold), foreign_reserve=D(short_foreign),
            buy_fee_rate=D("0"), sell_fee_rate=D("0"),
            short_lending_limit_foreign=D("10000000"))
        s.add(short_pair)
        await s.flush()
        s.add(FxTreasury(pair_id=short_pair.id, gold_balance=D(short_gold),
                         foreign_balance=D(short_foreign)))
        s.add(FxShortPosition(
            user_id=user.id, pair_id=short_pair.id,
            principal_foreign=D(short_principal), interest_foreign=D("0"),
            interest_last_accrued_at=now, restricted_gold=D("0"),
            proceeds_basis_gold=D("0")))
        asset_ids = []
        for _ in range(2):
            ap = FxPair(
                currency_code=uuid4().hex[:16], currency_name="A", status="trading",
                gold_reserve=D(asset_gold), foreign_reserve=D(asset_reserve),
                buy_fee_rate=D("0"), sell_fee_rate=D("0"))
            s.add(ap)
            await s.flush()
            s.add(FxTreasury(pair_id=ap.id, gold_balance=D(asset_gold),
                             foreign_balance=D(asset_reserve)))
            s.add(FxWallet(user_id=user.id, pair_id=ap.id,
                           foreign_amount=D(asset_foreign), cost_basis=D("0")))
            asset_ids.append(int(ap.id))
        await s.commit()
        return int(user.id), int(short_pair.id), asset_ids


async def test_rotation_sells_one_asset_retains_cash_then_covers_foreign_debt():
    """spec §8.1 零预算最大空头示例：卖一组、留现金、下一轮回补，不借新金债。"""
    uid, short_pid, asset_ids = await _seed_rotation()

    first = await liquidation_sweep.run_liquidation_sweep_once()
    assert first.get("monetary_action_count") == 1

    async with async_session_maker() as s:
        user = await s.get(User, uid)
        assert D(user.debt) == D("50"), "sale proceeds must not auto-repay gold debt"
        cash_after_sale = D(user.cash)
        assert cash_after_sale > D("0")
        wallets = {int(w.pair_id): D(w.foreign_amount)
                   for w in (await s.execute(select(FxWallet))).scalars()}
        assert sum(1 for pid in asset_ids if wallets[pid] == D("0")) == 1
        assert sum(1 for pid in asset_ids if wallets[pid] > D("0")) == 1
        short = (await s.execute(select(FxShortPosition).where(
            FxShortPosition.pair_id == short_pid))).scalars().one()
        assert D(short.principal_foreign) == D("10000"), "unpayable short untouched"
        actions = list((await s.execute(select(LiquidationAction).order_by(
            LiquidationAction.round_no))).scalars())
        assert len(actions) == 1
        assert actions[0].kind == "sell_group" and actions[0].repaid == 0
        run_id = int(actions[0].run_id)

    second = await liquidation_sweep.run_liquidation_sweep_once()
    assert second.get("monetary_action_count") == 1

    async with async_session_maker() as s:
        user = await s.get(User, uid)
        assert D(user.debt) == D("50"), "no gold repayment and no new gold loan"
        short = (await s.execute(select(FxShortPosition).where(
            FxShortPosition.pair_id == short_pid))).scalars().one()
        covered = D("10000") - D(short.principal_foreign)
        assert covered > 0
        assert D(short.interest_foreign) == D("0")
        actions = list((await s.execute(select(LiquidationAction).order_by(
            LiquidationAction.round_no))).scalars())
        assert [a.kind for a in actions] == ["sell_group", "cover_group"]
        cover = actions[1]
        assert int(cover.run_id) == run_id
        assert cover.product == "fx" and int(cover.group_id) == short_pid
        assert D(cover.foreign_repaid) == covered
        assert D(cover.gold_spent) == cash_after_sale - D(user.cash)
        assert cover.executed["limited_by_cash"] is True
        assert D(cover.short_after["principal_foreign"]) == D(short.principal_foreign)
        # Only one asset sold per round; the second is untouched by the cover.
        wallets = {int(w.pair_id): D(w.foreign_amount)
                   for w in (await s.execute(select(FxWallet))).scalars()}
        assert sum(1 for pid in asset_ids if wallets[pid] > D("0")) == 1
        # Physical foreign conservation on the covered pair.
        pair = await s.get(FxPair, short_pid)
        treasury = (await s.execute(select(FxTreasury).where(
            FxTreasury.pair_id == short_pid))).scalars().one()
        assert D(pair.foreign_reserve) + D(treasury.foreign_balance) == D("200000")
        assert D(pair.gold_reserve) == D("10000") + D(cover.gold_spent)


async def test_cover_group_records_actual_q_x_and_replay_scan_does_not_double_charge():
    """cover_group 落库真实 q/x/limited_by_cash；同轮重放与重复扫描不二次扣款。"""
    uid, pid, _ = await _seed_cover(
        cash="111.111112", principal="100", restricted="100", basis="100",
        gold="1000", foreign="1000", treasury_foreign="100000", buy_fee="0")

    first = await liquidation_sweep.run_liquidation_sweep_once()
    assert first.get("monetary_action_count") == 1

    async with async_session_maker() as s:
        action = (await s.execute(select(LiquidationAction))).scalars().one()
        run = await s.get(LiquidationRun, action.run_id)
        assert action.kind == "cover_group"
        assert action.product == "fx" and int(action.group_id) == pid
        assert D(action.foreign_repaid) == D("100")
        assert action.executed["limited_by_cash"] is False
        assert action.executed["full_cover"] is True
        assert D(action.short_after["principal_foreign"]) == D("0")
        assert D(action.short_after["restricted_gold"]) == D("0")
        short = (await s.execute(select(FxShortPosition).where(
            FxShortPosition.user_id == uid, FxShortPosition.pair_id == pid,
        ))).scalars().one()
        assert (short.principal_foreign, short.interest_foreign,
                short.restricted_gold, short.proceeds_basis_gold) == (D("0"),) * 4
        assert short.interest_last_accrued_at is None
        trade = (await s.execute(select(FxTrade).where(
            FxTrade.idempotency_key == f"liq:{run.id}:{action.round_no}"))).scalars().one()
        assert D(action.gold_spent) == D(trade.input_amount)
        assert D(action.foreign_repaid) == D(trade.output_amount)
        planned = str(trade.requested_foreign_amount)
        budget = str(trade.max_gold_in)
        run_id, round_no = int(run.id), int(action.round_no)
        before = await _cover_state(s, uid, pid)

    replay = await _liq_cover(uid, pid, run_id=run_id, round_no=round_no,
                              planned=planned, budget=budget)
    assert replay.replay is True

    async with async_session_maker() as s:
        assert await _cover_state(s, uid, pid) == before
        assert len(list((await s.execute(select(FxTrade))).scalars())) == 1
        assert len(list((await s.execute(select(LiquidationAction))).scalars())) == 1

    # After the full cover the run is recovered: a duplicate scan is inert.
    second = await liquidation_sweep.run_liquidation_sweep_once()
    assert second.get("monetary_action_count") == 0
    async with async_session_maker() as s:
        assert await _cover_state(s, uid, pid) == before
        assert len(list((await s.execute(select(FxTrade))).scalars())) == 1
        assert len(list((await s.execute(select(LiquidationAction))).scalars())) == 1


async def test_gold_repay_leaves_locked_short_s_untouched():
    """无可执行空头时自动还金债只花未锁现金，绝不消费空头锁金 S。"""
    uid, pid, _ = await _seed_cover(
        cash="200", principal="100", restricted="150", basis="150",
        gold="1000", foreign="1000", treasury_foreign="100000", buy_fee="0",
        debt="100", status="paused")

    result = await liquidation_sweep.run_liquidation_sweep_once()
    assert result.get("monetary_action_count") == 1

    async with async_session_maker() as s:
        user = await s.get(User, uid)
        short = (await s.execute(select(FxShortPosition).where(
            FxShortPosition.pair_id == pid))).scalars().one()
        # Only the unlocked 50 was spent; the 150 lock is intact and cash>=S.
        assert D(user.cash) == D("150")
        assert D(user.debt) == D("50")
        assert D(short.restricted_gold) == D("150")
        assert D(user.cash) >= D(short.restricted_gold)
        action = (await s.execute(select(LiquidationAction))).scalars().one()
        assert action.kind == "repay_cash" and D(action.repaid) == D("50")


async def _seed_sale_with_paused_short(*, cash="200", debt="300", restricted="150",
                                       short_principal="100",
                                       short_gold="1000", short_foreign="1000",
                                       asset_foreign="200",
                                       asset_gold="1000", asset_reserve="10000"):
    """一组可卖 FX 资产 + 一个已知定价但 ``paused`` 不可回补的空头 + 正金债。

    真实故障（spec §8.1 第 1/4 步）：卖出资产后到手的净回款已经是自由现金，但
    仍拿卖出**前**的 ``available_cash`` 当还金债上限，会把新现金当成不可用而少还
    金债、无谓地让 run 保持 active。这里 ``paused && !reduce_only`` 的空头定价
    已知（K 数值）但不可执行，因此不触发留存、也不该挡住按自由现金还债。
    """
    now = datetime.now(timezone.utc)
    async with async_session_maker() as s:
        user = User(username=uuid4().hex, casdoor_id=uuid4().hex,
                    cash=D(cash), debt=D(debt), debt_last_accrued_at=now)
        s.add(user)
        await s.flush()
        sp = FxPair(
            currency_code=uuid4().hex[:16], currency_name="S", status="paused",
            gold_reserve=D(short_gold), foreign_reserve=D(short_foreign),
            buy_fee_rate=D("0"), sell_fee_rate=D("0"),
            short_lending_limit_foreign=D("10000000"))
        s.add(sp)
        await s.flush()
        s.add(FxTreasury(pair_id=sp.id, gold_balance=D(short_gold),
                         foreign_balance=D(short_foreign)))
        s.add(FxShortPosition(
            user_id=user.id, pair_id=sp.id,
            principal_foreign=D(short_principal), interest_foreign=D("0"),
            interest_last_accrued_at=now, restricted_gold=D(restricted),
            proceeds_basis_gold=D(restricted)))
        ap = FxPair(
            currency_code=uuid4().hex[:16], currency_name="A", status="trading",
            gold_reserve=D(asset_gold), foreign_reserve=D(asset_reserve),
            buy_fee_rate=D("0"), sell_fee_rate=D("0"))
        s.add(ap)
        await s.flush()
        s.add(FxTreasury(pair_id=ap.id, gold_balance=D(asset_gold),
                         foreign_balance=D(asset_reserve)))
        s.add(FxWallet(user_id=user.id, pair_id=ap.id,
                       foreign_amount=D(asset_foreign), cost_basis=D("0")))
        await s.commit()
        return int(user.id), int(sp.id), int(ap.id)


async def test_sale_proceeds_repay_gold_debt_despite_paused_short():
    """paused 不可回补空头持有 S 时，卖出资产的净回款仍是可还债自由现金。"""
    uid, short_pid, asset_pid = await _seed_sale_with_paused_short()

    async with async_session_maker() as s:
        user_before = await s.get(User, uid)
        short_before = (await s.execute(select(FxShortPosition).where(
            FxShortPosition.pair_id == short_pid))).scalars().one()
        pre_sale_free = D(user_before.cash) - D(short_before.restricted_gold)
        assert pre_sale_free == D("50") < D(user_before.debt)

    result = await liquidation_sweep.run_liquidation_sweep_once()
    assert result.get("monetary_action_count") == 1

    async with async_session_maker() as s:
        user = await s.get(User, uid)
        short = (await s.execute(select(FxShortPosition).where(
            FxShortPosition.pair_id == short_pid))).scalars().one()
        actions = list((await s.execute(select(LiquidationAction))).scalars())
        assert [a.kind for a in actions] == ["sell_group"], "exactly one group/round"
        action = actions[0]
        assert action.product == "fx" and int(action.group_id) == asset_pid
        # The sale proceeds are spendable now: repay more than the pre-sale free cash.
        assert D(action.repaid) > pre_sale_free, "post-sale free cash must cap repayment"
        # Short lock S is never spent and stays intact.
        assert D(short.restricted_gold) == D("150")
        assert D(user.cash) >= D(short.restricted_gold), "locked S must remain covered"
        # Gold debt is reduced by exactly the recorded actual repayment.
        assert D(user.debt) == D("300") - D(action.repaid)
        # Foreign obligation untouched: paused short and its pair/treasury unchanged.
        assert D(short.principal_foreign) == D("100")
        assert D(short.interest_foreign) == D("0")
        paused_pair = await s.get(FxPair, short_pid)
        assert D(paused_pair.gold_reserve) == D("1000")
        assert D(paused_pair.foreign_reserve) == D("1000")
        paused_treasury = (await s.execute(select(FxTreasury).where(
            FxTreasury.pair_id == short_pid))).scalars().one()
        assert D(paused_treasury.gold_balance) == D("1000")
        assert D(paused_treasury.foreign_balance) == D("1000")
        # The sold asset wallet is empty; no cover was attempted.
        wallet = (await s.execute(select(FxWallet).where(
            FxWallet.pair_id == asset_pid))).scalars().one()
        assert D(wallet.foreign_amount) == D("0")


async def _seed_lmsr_with_unpayable_short(*, debt="100", shares="100",
                                          pair_status="trading"):
    now = datetime.now(timezone.utc)
    async with async_session_maker() as s:
        user = User(username=uuid4().hex, casdoor_id=uuid4().hex,
                    cash=D("0"), debt=D(debt), debt_last_accrued_at=now)
        s.add(user)
        await s.flush()
        sp = FxPair(
            currency_code=uuid4().hex[:16], currency_name="S", status=pair_status,
            gold_reserve=D("10000"), foreign_reserve=D("100000"),
            buy_fee_rate=D("0"), sell_fee_rate=D("0"),
            short_lending_limit_foreign=D("10000000"))
        s.add(sp)
        await s.flush()
        s.add(FxTreasury(pair_id=sp.id, gold_balance=D("10000"),
                         foreign_balance=D("100000")))
        s.add(FxShortPosition(
            user_id=user.id, pair_id=sp.id, principal_foreign=D("10000"),
            interest_foreign=D("0"), interest_last_accrued_at=now,
            restricted_gold=D("0"), proceeds_basis_gold=D("0")))
        market = Market(title=uuid4().hex[:12], liquidity_b=100,
                        status=MarketStatus.TRADING)
        s.add(market)
        await s.flush()
        a = Outcome(market_id=market.id, label="a", total_shares=D(shares))
        b = Outcome(market_id=market.id, label="b", total_shares=D("0"))
        s.add_all([a, b])
        await s.flush()
        s.add(Position(user_id=user.id, outcome_id=a.id, amount=D(shares),
                       cost_basis=D("0")))
        await s.commit()
        return int(user.id), int(sp.id), int(market.id)


async def test_lmsr_sale_with_pending_short_retains_proceeds_for_cover():
    """LMSR writer 路线同样留存净回款：有待回补空头时不自动还金债。"""
    from app.services.credit.execution import execute_user
    from app.services.market_writer import WRITER

    uid, short_pid, _market_id = await _seed_lmsr_with_unpayable_short()
    await WRITER.start()
    try:
        status = await execute_user(uid, rate=D("0"), pct=D("0.1"), source="scheduler")
    finally:
        await WRITER.stop()

    assert status == "triggered"
    async with async_session_maker() as s:
        user = await s.get(User, uid)
        assert D(user.debt) == D("100"), "LMSR proceeds must not auto-repay gold debt"
        assert D(user.cash) > D("0"), "proceeds retained as free cash"
        assert (await s.execute(select(Position))).scalars().first() is None
        short = (await s.execute(select(FxShortPosition).where(
            FxShortPosition.pair_id == short_pid))).scalars().one()
        assert D(short.principal_foreign) == D("10000")
        action = (await s.execute(select(LiquidationAction))).scalars().one()
        assert action.kind == "sell_group" and action.product == "lmsr"
        assert action.repaid == 0


async def test_lmsr_sale_with_paused_short_repays_gold_from_proceeds():
    """LMSR writer 路线：paused 不可回补空头不触发留存，净回款按自由现金还金债。"""
    from app.services.credit.execution import execute_user
    from app.services.market_writer import WRITER

    uid, short_pid, _market_id = await _seed_lmsr_with_unpayable_short(pair_status="paused")
    await WRITER.start()
    try:
        status = await execute_user(uid, rate=D("0"), pct=D("0.1"), source="scheduler")
    finally:
        await WRITER.stop()

    assert status == "triggered"
    async with async_session_maker() as s:
        user = await s.get(User, uid)
        action = (await s.execute(select(LiquidationAction))).scalars().one()
        assert action.kind == "sell_group" and action.product == "lmsr"
        # Pre-sale free cash is zero; the proceeds themselves must repay gold debt.
        assert D(action.repaid) > D("0"), "post-sale proceeds must repay gold debt"
        assert D(user.debt) == D("100") - D(action.repaid)
        assert D(user.cash) >= D("0")
        short = (await s.execute(select(FxShortPosition).where(
            FxShortPosition.pair_id == short_pid))).scalars().one()
        assert D(short.principal_foreign) == D("10000")
        assert D(short.restricted_gold) == D("0")


async def test_queued_lmsr_sale_revalidation_persists_unknown_short_block(monkeypatch):
    """A selected LMSR sale must commit its blocked response if K becomes unknown."""
    from app.services.credit.execution import execute_user
    from app.services.market_writer import WRITER

    uid, pair_id, _ = await _seed_lmsr_with_unpayable_short()
    submit = WRITER.submit
    responses = []

    async def submit_after_pool_drift(cmd):
        # Gates are released before submission: another pair transaction can
        # exhaust the full-cover reserve before writer revalidation.
        async with async_session_maker() as s:
            pair = await s.get(FxPair, pair_id)
            pair.foreign_reserve = D('10000')
            await s.commit()
        response = await submit(cmd)
        responses.append(response)
        return response

    monkeypatch.setattr(WRITER, 'submit', submit_after_pool_drift)
    await WRITER.start()
    try:
        status = await execute_user(uid, rate=D('0'), pct=D('.1'), source='scheduler')
    finally:
        await WRITER.stop()

    assert status == 'blocked'
    assert responses[0]['blocked_reason'] == 'insufficient_pool_foreign'
    assert responses[0]['sold_count'] == 0
    async with async_session_maker() as s:
        run = (await s.execute(select(LiquidationRun))).scalar_one()
        action = (await s.execute(select(LiquidationAction))).scalar_one()
        assert run.status == 'active'
        assert run.last_blocked_reason == 'insufficient_pool_foreign'
        assert action.kind == 'blocked'
        assert action.blocked_reason == 'insufficient_pool_foreign'
        assert action.group_id == pair_id
        user = await s.get(User, uid)
        assert user.cash == 0 and user.debt == D('100')
        assert (await s.execute(select(Position))).scalar_one().amount == D('100')
        assert not list((await s.execute(select(LiquidationEvent))).scalars())


async def test_finish_locked_keeps_foreign_only_unknown_post_state_active():
    """Post-action revaluation cannot recover a live foreign obligation at D=0."""
    from app.services.credit.execution import finish_locked
    from app.services.credit.runs import get_or_create_active_run

    uid, pair_id = await _seed_foreign_only_overflow()
    async with async_session_maker() as s:
        run = await get_or_create_active_run(
            s, user_id=uid, trigger_source='scheduler', now=datetime.now(timezone.utc))
        await s.commit()
        run_id = run.id

    async with async_session_maker() as s:
        user = await s.get(User, uid)
        run = await s.get(LiquidationRun, run_id)
        await finish_locked(s, user, run, D('0'))
        await s.commit()

    async with async_session_maker() as s:
        run = await s.get(LiquidationRun, run_id)
        assert run.status == 'active'
        assert run.last_blocked_reason == 'insufficient_pool_foreign'
        assert run.closed_at is None
        short = (await s.execute(select(FxShortPosition).where(
            FxShortPosition.pair_id == pair_id))).scalar_one()
        assert short.principal_foreign == D('1000')
        user = await s.get(User, uid)
        assert user.debt == 0 and user.cash == D('50')


@pytest.mark.parametrize('cash, expected_limited', [('600000000', False), ('100000000', True)])
async def test_unknown_full_quote_overflow_covers_only_fixed_budgeted_short(cash, expected_limited):
    """A storable partial cover must reduce an overflow debt without selling collateral.

    Covers both an affordable fixed 10% batch and an exact-input cash fallback;
    persisted physical gold/foreign legs must conserve value, with K still unknown.
    """
    from app.services.credit.valuation import value_user_detailed

    uid, pid = await _seed_foreign_only_overflow(
        principal='500', cash=cash, with_asset=True)
    async with async_session_maker() as s:
        pair = await s.get(FxPair, pid)
        pair.gold_reserve = D('9000000000')
        user = await s.get(User, uid)
        user.debt = D('100')
        await s.commit()
        pre = await value_user_detailed(s, uid, daily_rate=D('0'))
        assert pre.risk_status == 'blocked' and pre.short_cover_cost is None
        assert pre.blocked_reason == 'short_quote_failed'

    result = await liquidation_sweep.run_liquidation_sweep_once()
    assert result.get('monetary_action_count') == 1
    async with async_session_maker() as s:
        run = (await s.execute(select(LiquidationRun))).scalar_one()
        assert run.status == 'active'
        assert run.pre_liquidation_equity is None and run.pre_risk_basis is None
        action = (await s.execute(select(LiquidationAction))).scalar_one()
        assert action.kind == 'cover_group' and action.group_id == pid
        assert action.mode == 'partial' and action.repaid == 0
        assert D(action.requested['planned_amount']) == D('50')
        assert action.executed['limited_by_cash'] is expected_limited
        assert D(action.executed['repaid_foreign']) == action.foreign_repaid
        assert D(action.executed['paid_gold']) == action.gold_spent
        assert 0 < action.foreign_repaid <= D('50')
        assert 0 < action.gold_spent <= D(cash)
        assert not action.executed['full_cover']
        user = await s.get(User, uid)
        pair = await s.get(FxPair, pid)
        short = (await s.execute(select(FxShortPosition))).scalar_one()
        treasury = (await s.execute(select(FxTreasury))).scalar_one()
        assert user.debt == D('100')
        assert user.cash + pair.gold_reserve + treasury.gold_balance == D(cash) + D('9000001000')
        assert pair.foreign_reserve + treasury.foreign_balance == D('2000')
        assert short.principal_foreign + action.foreign_repaid == D('500')
        assert short.restricted_gold == 0 and short.interest_foreign == 0
        assert (await s.execute(select(Position))).scalar_one().amount == D('10')
        post = await value_user_detailed(s, uid, daily_rate=D('0'))
        assert post.short_cover_cost is None and post.liquidation_equity is None
        assert post.risk_basis is None and run.last_blocked_reason == 'short_quote_failed'
        run_id = run.id
    # A subsequent scan reuses the active run; it never recovers from D alone.
    await liquidation_sweep.run_liquidation_sweep_once()
    async with async_session_maker() as s:
        runs = list((await s.execute(select(LiquidationRun))).scalars())
        assert len(runs) == 1 and runs[0].id == run_id and runs[0].status == 'active'
        assert (await s.get(User, uid)).debt == D('100')
        assert (await s.execute(select(Position))).scalar_one().amount == D('10')


async def test_unknown_overflow_skips_unstorable_treasury_and_covers_next_pair():
    """An unreturnable first inventory must not starve a later legal cover."""
    uid, first_id = await _seed_foreign_only_overflow(
        principal='500', cash='600000000', with_asset=True)
    async with async_session_maker() as s:
        first = await s.get(FxPair, first_id)
        first.gold_reserve = D('9000000000')
        user = await s.get(User, uid)
        user.debt = D('100')
        first_treasury = (await s.execute(select(FxTreasury).where(
            FxTreasury.pair_id == first_id))).scalar_one()
        first_treasury.foreign_balance = D('9999999990')
        second = FxPair(currency_code='OV2', currency_name='Second overflow',
            status='trading', gold_reserve=D('9000000000'),
            foreign_reserve=D('1000'), buy_fee_rate=D('0'), sell_fee_rate=D('0'))
        s.add(second)
        await s.flush()
        second_id = second.id
        s.add(FxTreasury(pair_id=second_id, gold_balance=D('1000'),
                         foreign_balance=D('1000')))
        s.add(FxShortPosition(user_id=uid, pair_id=second_id,
            principal_foreign=D('500'), interest_foreign=D('0'),
            interest_last_accrued_at=datetime.now(timezone.utc),
            restricted_gold=D('0'), proceeds_basis_gold=D('0')))
        await s.commit()

    result = await liquidation_sweep.run_liquidation_sweep_once()
    assert result.get('monetary_action_count') == 1
    assert result.get('errors') == 0
    async with async_session_maker() as s:
        action = (await s.execute(select(LiquidationAction))).scalar_one()
        assert action.kind == 'cover_group' and action.group_id == second_id
        assert action.foreign_repaid == D('50') and action.repaid == 0
        run = (await s.execute(select(LiquidationRun))).scalar_one()
        assert run.status == 'active' and run.pre_liquidation_equity is None
        first = await s.get(FxPair, first_id)
        assert first.gold_reserve == D('9000000000') and first.foreign_reserve == D('1000')
        first_short = (await s.execute(select(FxShortPosition).where(
            FxShortPosition.pair_id == first_id))).scalar_one()
        assert first_short.principal_foreign == D('500') and first_short.interest_foreign == 0
        first_treasury = (await s.execute(select(FxTreasury).where(
            FxTreasury.pair_id == first_id))).scalar_one()
        assert first_treasury.gold_balance == D('1000')
        assert first_treasury.foreign_balance == D('9999999990')
        second = await s.get(FxPair, second_id)
        second_treasury = (await s.execute(select(FxTreasury).where(
            FxTreasury.pair_id == second_id))).scalar_one()
        user = await s.get(User, uid)
        assert user.debt == D('100')
        assert user.cash + action.gold_spent == D('600000000')
        assert user.cash + second.gold_reserve + second_treasury.gold_balance == D('9600001000')
        assert second.foreign_reserve + second_treasury.foreign_balance == D('2000')
        assert (await s.execute(select(Position))).scalar_one().amount == D('10')
        trades = list((await s.execute(select(FxTrade))).scalars())
        assert len(trades) == 1 and trades[0].pair_id == second_id


async def test_complete_foreign_insolvency_preserves_debt_and_resumes_after_deposit():
    uid, pid, _ = await _seed_cover(
        cash="0", principal="100", interest="2", restricted="0", basis="0",
        gold="1000", foreign="1000", treasury_foreign="100000", buy_fee="0")
    await liquidation_sweep.run_liquidation_sweep_once()
    async with async_session_maker() as s:
        run = (await s.execute(select(LiquidationRun))).scalar_one()
        assert run.status == "insolvent"
        user = await s.get(User, uid)
        assert user.credit_frozen and D(user.cash) == 0 and D(user.debt) == 0
        short = (await s.execute(select(FxShortPosition))).scalar_one()
        assert D(short.principal_foreign) == 100 and D(short.interest_foreign) == 2
        treasury = (await s.execute(select(FxTreasury))).scalar_one()
        assert D(treasury.foreign_balance) == 100000 and D(treasury.gold_balance) == 1000
    for _ in range(2):
        await liquidation_sweep.run_liquidation_sweep_once()
    async with async_session_maker() as s:
        assert (await s.execute(select(func.count(LiquidationRun.id)))).scalar_one() == 1
        user = await s.get(User, uid)
        user.cash = D("20")
        await s.commit()
    result = await liquidation_sweep.run_liquidation_sweep_once()
    assert result["monetary_action_count"] == 1
    async with async_session_maker() as s:
        runs = list((await s.execute(select(LiquidationRun).order_by(LiquidationRun.id))).scalars())
        assert len(runs) == 2 and runs[0].status == "insolvent"
        short = (await s.execute(select(FxShortPosition))).scalar_one()
        assert 0 < D(short.principal_foreign) < 100 and D(short.interest_foreign) == 0
        assert (await s.get(User, uid)).credit_frozen


async def test_complete_paused_short_with_no_resources_waits_for_market():
    uid, pid, _ = await _seed_cover(
        cash="0", principal="100", restricted="0", basis="0",
        gold="1000", foreign="1000", status="paused", buy_fee="0")
    await liquidation_sweep.run_liquidation_sweep_once()
    async with async_session_maker() as s:
        run = (await s.execute(select(LiquidationRun))).scalar_one()
        assert run.status == "active" and run.pre_risk_basis is not None
        assert run.last_blocked_reason == "pair_paused"
        assert not (await s.get(User, uid)).credit_frozen


@pytest.mark.parametrize("cash,expected", [("2.085", "active"), ("2.12", "recovered")])
async def test_unknown_run_recovers_at_initial_only_after_pool_funding(cash, expected):
    uid, pid = await _seed_foreign_only_overflow(principal="2", foreign_reserve="2", cash=cash)
    await liquidation_sweep.run_liquidation_sweep_once()
    async with async_session_maker() as s:
        pair = await s.get(FxPair, pid)
        pair.foreign_reserve = D("1000")
        pair.status = "paused"
        # Explicit pool funding repairs full-cover quotation; no cover trade.
        await s.commit()
    await liquidation_sweep.run_liquidation_sweep_once()
    async with async_session_maker() as s:
        run = (await s.execute(select(LiquidationRun))).scalar_one()
        assert run.status == expected
        short = (await s.execute(select(FxShortPosition))).scalar_one()
        assert D(short.principal_foreign) == 2 and D(short.interest_foreign) == 0
        assert D((await s.get(User, uid)).cash) == D(cash)


async def test_known_cover_skips_unstorable_treasury_and_executes_next_short():
    """A high-ranked inventory overflow must not starve a legal lower-ranked short."""
    from app.services.credit.valuation import value_user_detailed

    uid, first_id = await _seed_foreign_only_overflow(principal='100', cash='100')
    async with async_session_maker() as s:
        first_treasury = (await s.execute(select(FxTreasury).where(
            FxTreasury.pair_id == first_id))).scalar_one()
        first_treasury.foreign_balance = D('9999999990')
        second = FxPair(currency_code='KN2', currency_name='Second known short',
            status='trading', gold_reserve=D('1000'), foreign_reserve=D('1000'),
            buy_fee_rate=D('0'), sell_fee_rate=D('0'))
        s.add(second)
        await s.flush()
        second_id = second.id
        s.add(FxTreasury(pair_id=second_id, gold_balance=D('1000'),
                         foreign_balance=D('1000')))
        s.add(FxShortPosition(user_id=uid, pair_id=second_id,
            principal_foreign=D('10'), interest_foreign=D('0'),
            interest_last_accrued_at=datetime.now(timezone.utc),
            restricted_gold=D('0'), proceeds_basis_gold=D('0')))
        await s.commit()
        pre = await value_user_detailed(s, uid, daily_rate=D('0'))
        assert pre.risk_status == 'ok' and pre.liquidation_equity < 0

    result = await liquidation_sweep.run_liquidation_sweep_once()
    assert result.get('monetary_action_count') == 1 and result.get('errors') == 0
    async with async_session_maker() as s:
        action = (await s.execute(select(LiquidationAction))).scalar_one()
        assert action.kind == 'cover_group' and action.group_id == second_id
        assert action.mode == 'full' and action.foreign_repaid == D('10')
        assert action.repaid == 0 and not action.executed['limited_by_cash']
        first = await s.get(FxPair, first_id)
        assert first.gold_reserve == D('1000') and first.foreign_reserve == D('1000')
        first_short = (await s.execute(select(FxShortPosition).where(
            FxShortPosition.pair_id == first_id))).scalar_one()
        assert first_short.principal_foreign == D('100') and first_short.interest_foreign == 0
        first_treasury = (await s.execute(select(FxTreasury).where(
            FxTreasury.pair_id == first_id))).scalar_one()
        assert first_treasury.gold_balance == D('1000')
        assert first_treasury.foreign_balance == D('9999999990')
        second = await s.get(FxPair, second_id)
        second_treasury = (await s.execute(select(FxTreasury).where(
            FxTreasury.pair_id == second_id))).scalar_one()
        second_short = (await s.execute(select(FxShortPosition).where(
            FxShortPosition.pair_id == second_id))).scalar_one()
        assert second_short.principal_foreign == 0 and second_short.interest_foreign == 0
        user = await s.get(User, uid)
        assert user.debt == 0 and user.cash + action.gold_spent == D('100')
        assert user.cash + second.gold_reserve + second_treasury.gold_balance == D('2100')
        assert second.foreign_reserve + second_treasury.foreign_balance == D('2000')
        trades = list((await s.execute(select(FxTrade))).scalars())
        assert len(trades) == 1 and trades[0].pair_id == second_id


async def test_treasury_operations_preserve_borrowed_stock_and_unknown_debt_scan():
    """A reserve withdrawal can make K unknown without erasing borrowed coins."""
    from app.services.fx import liquidity
    uid, pair_id = await _seed_foreign_only_overflow(principal='900', cash='50')
    async with async_session_maker() as s:
        short = (await s.execute(select(FxShortPosition))).scalar_one()
        short.interest_foreign = D('5')
        short.restricted_gold = D('20')
        short.proceeds_basis_gold = D('20')
        treasury = (await s.execute(select(FxTreasury))).scalar_one()
        # Borrowing consumed real treasury stock, independently of pool reserves.
        treasury.foreign_balance -= short.principal_foreign
        await s.commit()
        pair = await s.get(FxPair, pair_id)
        original_version = pair.pool_version
        await liquidity.fund_pair(s, pair_id, D('10'), D('20'), uid)
        await liquidity.withdraw_pair(s, pair_id, D('0'), D('120'), uid)
        await s.refresh(pair)
        assert pair.pool_version == original_version + 2
        assert pair.gold_reserve == D('1010') and pair.foreign_reserve == D('900')
        treasury = (await s.execute(select(FxTreasury))).scalar_one()
        assert treasury.gold_balance == D('1010') and treasury.foreign_balance == D('0')

    result = await liquidation_sweep.run_liquidation_sweep_once()
    assert result.get('blocked_count') == 1 and result.get('monetary_action_count') == 0
    async with async_session_maker() as s:
        run = (await s.execute(select(LiquidationRun))).scalar_one()
        assert run.status == 'active'
        assert run.pre_liquidation_equity is None and run.pre_risk_basis is None
        action = (await s.execute(select(LiquidationAction))).scalar_one()
        assert action.kind == 'blocked' and action.blocked_reason == 'insufficient_pool_foreign'
        short = (await s.execute(select(FxShortPosition))).scalar_one()
        assert (short.principal_foreign, short.interest_foreign, short.restricted_gold,
                short.proceeds_basis_gold) == (D('900'), D('5'), D('20'), D('20'))
        user = await s.get(User, uid)
        assert user.cash == D('50') and user.debt == D('0')
        treasury = (await s.execute(select(FxTreasury))).scalar_one()
        assert treasury.foreign_balance == D('0') and treasury.gold_balance == D('1010')
        pair = await s.get(FxPair, pair_id)
        assert pair.foreign_reserve == D('900') and pair.pool_version == original_version + 2
        audits = list((await s.execute(select(AuditEvent).where(
            AuditEvent.event_type.in_(('fx_fund', 'fx_withdraw'))))).scalars())
        assert len(audits) == 2


async def test_blocked_run_resumes_cover_with_current_provenance():
    """A paused obligation resumes on the same run with usable start provenance."""
    uid, pid, _ = await _seed_cover(
        cash="114", principal="100", restricted="20", basis="30",
        gold="1000", foreign="1000", treasury_foreign="100000", buy_fee="0")
    async with async_session_maker() as s:
        pair = await s.get(FxPair, pid)
        pair.status = "paused"
        await s.commit()
    await liquidation_sweep.run_liquidation_sweep_once()
    async with async_session_maker() as s:
        run = (await s.execute(select(LiquidationRun))).scalars().one()
        run_id = run.id
        assert run.last_blocked_reason == "pair_paused"
        assert run.margin_version == 2
        assert run.pre_risk_basis > 0
        assert run.pre_equity_to_risk_basis is not None
        snapshot = run.pre_short_positions[str(pid)]
        assert D(snapshot["principal_foreign"]) == D("100")
        assert D(snapshot["restricted_gold"]) == D("20")
        pair = await s.get(FxPair, pid)
        pair.status = "trading"
        await s.commit()
    result = await liquidation_sweep.run_liquidation_sweep_once()
    assert result.get("monetary_action_count") == 1
    async with async_session_maker() as s:
        run = await s.get(LiquidationRun, run_id)
        assert run.status == "active"
        assert run.last_blocked_reason is None
        actions = list((await s.execute(select(LiquidationAction).where(
            LiquidationAction.run_id == run_id).order_by(LiquidationAction.round_no))).scalars())
        assert [a.kind for a in actions] == ["blocked", "cover_group"]


async def test_lmsr_last_asset_audit_records_final_insolvency_version():
    """The action audit describes the committed freeze, not its intermediate version."""
    from app.services.credit.execution import execute_user
    from app.services.market_writer import WRITER

    async with async_session_maker() as s:
        user = User(username=uuid4().hex, casdoor_id=uuid4().hex,
                    cash=D("0"), debt=D("200"),
                    debt_last_accrued_at=datetime.now(timezone.utc))
        market = Market(title=uuid4().hex[:12], liquidity_b=100,
                        status=MarketStatus.TRADING)
        s.add_all([user, market])
        await s.flush()
        a = Outcome(market_id=market.id, label="a", total_shares=D("100"))
        b = Outcome(market_id=market.id, label="b", total_shares=D("0"))
        s.add_all([a, b])
        await s.flush()
        s.add(Position(user_id=user.id, outcome_id=a.id,
                       amount=D("100"), cost_basis=D("0")))
        uid = int(user.id)
        await s.commit()

    await WRITER.start()
    try:
        assert await execute_user(uid, rate=D("0"), pct=D(".1"),
                                  source="scheduler") == "triggered"
    finally:
        await WRITER.stop()

    async with async_session_maker() as s:
        user = await s.get(User, uid)
        assert user.credit_frozen and user.debt > 0 and user.cash == 0
        assert not list((await s.execute(select(Position).where(
            Position.user_id == uid))).scalars())
        run = (await s.execute(select(LiquidationRun).where(
            LiquidationRun.user_id == uid))).scalars().one()
        assert run.status == "insolvent"
        action = (await s.execute(select(LiquidationAction).where(
            LiquidationAction.run_id == run.id))).scalars().one()
        audit = (await s.execute(select(AuditEvent).where(
            AuditEvent.event_type == "liquidation_action",
            AuditEvent.user_id == uid))).scalars().one()
        assert action.kind == "sell_group" and action.product == "lmsr"
        assert (user.economic_version == action.economic_version_after
                == audit.payload["economic_version_after"])
        assert audit.ref_id == action.id
        assert D(audit.user_after["cash"]) == user.cash
        assert D(audit.user_after["debt"]) == user.debt
        events = list((await s.execute(select(LiquidationEvent).where(
            LiquidationEvent.user_id == uid))).scalars())
        assert len(events) == 1 and run.rounds == 1
