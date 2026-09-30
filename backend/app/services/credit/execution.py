"""One scheduled liquidation batch. Queue/gate waits never retain DB resources."""
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import select
from app.core.database import async_session_maker
from app.models.base import User, LiquidationEvent
from app.models.credit import LiquidationRun
from app.models.fx import FxPair
from app.services import loan_service, audit_service
from app.services.credit.flags import get_flags
from app.services.credit.gates import GATES
from app.services.credit.keys import GroupKey
from app.services.credit.risk import discover_dependencies
from app.services.credit.runs import (
    close_run,
    find_action_for_round,
    find_latest_action,
    get_or_create_active_run,
    record_action,
)
from app.services.credit.valuation import RISK_STATUS_OK, value_user_detailed
from app.services.credit.fx_quote import quote_fx_group
from app.services.credit.lmsr_quote import quote_lmsr_group
from app.services.credit.ownership import OWNERSHIP
from app.services.credit.version import bump_economic_version
from app.services.market_locks import lock_user

ZERO = Decimal('0')
#: WP4a 已知完整估值的空头只维持 run，真正的资金顺序/组选择在 WP4c/d。
SHORT_DEFERRED_REASON = 'short_cover_deferred'


def _has_foreign_obligation(pre):
    """估值里存在空头组 ⇔ 该账户有正的外币本金/利息（spec §8.2）。"""
    return any(group.role == 'short_cover' for group in pre.groups)


def _blocked_short_group(pre, reason):
    """*reason* 真正归属的空头 pair；无法唯一确定时返回 ``None``。

    ``pre.blocked_reason`` 是估值聚合出的逗号分隔原因集合，而每个
    ``short_cover`` 组各自只带一条 ``blocked_reason``。按排序取第一个空头会把
    健康/最高 K 的 pair 记到别的 pair 的失败原因旁，而且 K 排序漂移会让同一
    逻辑阻塞换 pair、重复记 blocked 轮。只有恰好一个空头组自身原因出现在
    *reason* 中时才归属到它；全局原因（如 ``restricted_cash_exceeds_cash``）
    或多个同原因 pair 一律返回 ``None``，由调用方中性记录，绝不认领健康 pair。
    """
    reasons = {part for part in (reason or '').split(',') if part}
    if not reasons:
        return None
    matched = [group for group in pre.groups
               if group.role == 'short_cover' and group.blocked_reason in reasons]
    return matched[0] if len(matched) == 1 else None


def _positive_assets(pre):
    return sum(
        (group.value for group in pre.groups
         if group.role == 'asset_sale' and group.value is not None),
        ZERO,
    )


def _triggered(pre, thresholds):
    """已知完整估值才比较：共享 E/B 门槛，K 取自权威估值（spec §6.1/§8.1）。"""
    return thresholds.triggered_basis(
        equity=pre.liquidation_equity,
        debt=pre.debt_effective,
        positive_assets=_positive_assets(pre),
        short_cover=pre.short_cover_cost,
    )


def _defer_short(pre):
    """未知 K 或存在外币义务：WP4a 一律不选资产、不做资金动作。"""
    return pre.risk_status != RISK_STATUS_OK or _has_foreign_obligation(pre)


def _seed_run_snapshot(run, pre):
    """run 起点快照；未知 E/B 时保持 None，绝不写成 0。"""
    run.pre_cash = pre.cash
    run.pre_debt = pre.debt_effective
    run.pre_liquidation_equity = pre.liquidation_equity
    run.pre_risk_basis = pre.risk_basis


async def _record_blocked_once(session, user, run, *, group, reason):
    """同一 run 连续阻塞只记一轮：重复扫描不堆积 blocked action、不写经济状态。"""
    product = group.key.product if group is not None else None
    group_id = group.key.group_id if group is not None else None
    last = await find_latest_action(session, run=run)
    if (last is not None and last.kind == 'blocked'
            and last.blocked_reason == reason
            and last.product == product
            and last.group_id == group_id):
        return last
    return await record_action(
        session, run=run, round_no=run.next_round, kind='blocked',
        product=product, group_id=group_id, blocked_reason=reason,
        debt_after=user.debt, cash_after=user.cash,
        economic_version_after=user.economic_version,
    )


async def repay(session, user, rate, source):
    before = user.debt
    before_clock = user.debt_last_accrued_at
    now = loan_service._compat_now(user)
    loan_service.accrue_interest(user, rate, now)
    amount = min(max(user.cash, ZERO), user.debt)
    if amount <= 0:
        if user.debt != before or user.debt_last_accrued_at != before_clock:
            bump_economic_version(user)
        if user.debt != before:
            audit_service.record(session, "interest_accrual", user_id=user.id,
                payload={"debt_before": before, "debt_after": user.debt,
                         "interest": user.debt-before, "daily_rate": rate, "source": source},
                user_after=audit_service.user_snapshot(user))
        return ZERO
    paid = await loan_service.decrease_debt_locked(session, user, amount, consume_cash=True,
                                                  daily_rate=rate, now=now)
    audit_service.record_liquidation_repay(session, user, paid, before, rate, source)
    return paid


def public_event(session, user, run, pre, *, product, mode, sold, proceeds, repaid, source):
    if not sold and repaid <= 0:
        return
    now = datetime.now(timezone.utc)
    user.last_liquidated_at = now
    session.add(LiquidationEvent(user_id=user.id, run_id=run.id, product=product,
        triggered_at=now, pre_cash=pre.cash, pre_debt=pre.debt_effective,
        pre_holdings_value=pre.liquidation_equity + pre.debt_effective - pre.cash,
        pre_net_worth=pre.liquidation_equity,
        pre_margin_ratio=pre.liquidation_equity/pre.debt_effective if pre.debt_effective else None,
        sold_positions_count=sold, total_proceeds=proceeds, repaid_amount=repaid,
        remaining_debt=user.debt, post_cash=user.cash, trigger_source=source, mode=mode))


def choose_group(pre, deps, pct):
    if pre.liquidation_equity is None:
        return None, None
    mode = 'full' if pre.liquidation_equity <= 0 else 'partial'
    for group in pre.groups:
        # 空头组不是正资产回收：不塞进资产卖出过滤器（spec §8.1 第 2 步）。
        if group.role != 'asset_sale' or not group.executable or group.value <= 0:
            continue
        snap = deps.snapshots[group.key]
        holdings = deps.holdings[group.key]
        if group.key.product == 'fx':
            q = quote_fx_group(snap.pair, foreign_amount=sum(holdings.values(), ZERO),
                               mode=mode, partial_pct=pct)
            executable = q.blocked_reason is None and q.gold_out > 0
        else:
            q = quote_lmsr_group(snap.outcomes, holdings, market_id=group.key.group_id,
                b=snap.b, fee_rate=snap.fee_rate, mode=mode, partial_pct=pct)
            executable = q.blocked_reason is None and q.net > 0 and bool(q.legs)
        if executable:
            return group.key, mode
    return None, mode


async def prepare_locked(session, user, deps, *, rate, pct, source, run_id=None, round_no=None, pre=None):
    """Fresh, fully gated account check; returns (run, pre, target, mode, status).

    Cash is only applied here if no sale is needed. Otherwise the selected
    executor repays all cash in its atomic sale/action transaction.
    """
    OWNERSHIP.require_writes()
    if pre is None:
        pre = await value_user_detailed(session, user.id, daily_rate=rate)
    run = (await session.execute(select(LiquidationRun).where(
        LiquidationRun.user_id == user.id, LiquidationRun.status == 'active')
        .with_for_update().execution_options(populate_existing=True))).scalars().first()
    if run_id is not None:
        expected = await session.get(LiquidationRun, run_id)
        if expected is None or expected.user_id != user.id:
            raise ValueError('invalid liquidation run')
        old = await find_action_for_round(session, run=expected, round_no=round_no)
        if old is not None:
            return expected, pre, None, None, 'replayed'
        if run is None or run.id != run_id or run.next_round != round_no:
            raise ValueError('stale liquidation round')
    thresholds = get_flags().thresholds

    # 未知 K / 数据错误：不做任何数值比较或恢复判定；建/续唯一 active run 并记录原因。
    if pre.risk_status != RISK_STATUS_OK:
        created = run is None
        if run is None:
            run = await get_or_create_active_run(session, user_id=user.id,
                trigger_source=source, now=datetime.now(timezone.utc))
        if created:
            _seed_run_snapshot(run, pre)
            await session.flush()
        await _record_blocked_once(session, user, run,
            group=_blocked_short_group(pre, pre.blocked_reason),
            reason=pre.blocked_reason or 'risk_unquotable')
        return run, pre, None, None, 'blocked'

    if run is None and not _triggered(pre, thresholds):
        return None, pre, None, None, 'recovered'
    if run is None:
        run = await get_or_create_active_run(session, user_id=user.id, trigger_source=source,
                                             now=datetime.now(timezone.utc))
        _seed_run_snapshot(run, pre)
        await session.flush()

    # 已知完整估值的空头：资金顺序/组选择属 WP4c/d；此处只维持 active run 不丢状态。
    # 通用 deferred 原因不属于任何单个 pair，不能按排序认领一个（否则 idempotency 随 K 漂移）。
    if _has_foreign_obligation(pre):
        await _record_blocked_once(session, user, run,
            group=_blocked_short_group(pre, SHORT_DEFERRED_REASON),
            reason=SHORT_DEFERRED_REASON)
        return run, pre, None, None, 'blocked'

    if thresholds.recovered(pre.liquidation_equity, pre.debt_effective):
        await close_run(session, run=run, status='recovered', now=datetime.now(timezone.utc))
        return run, pre, None, None, 'recovered'
    cash_repay = min(max(pre.cash, ZERO), pre.debt_effective)
    cash_recovers = thresholds.recovered(pre.liquidation_equity, pre.debt_effective-cash_repay)
    target, mode = choose_group(pre, deps, pct)
    if cash_recovers or target is None:
        paid = await repay(session, user, rate, source)
        await session.flush()
        after_cash = await value_user_detailed(session, user.id, daily_rate=rate)
        cash_recovers = thresholds.recovered(after_cash.liquidation_equity, after_cash.debt_effective)
        if target is None and not cash_recovers and pre.liquidation_equity <= 0:
            user.credit_frozen = True
            bump_economic_version(user)
        await record_action(session, run=run, round_no=run.next_round,
            kind='repay_cash' if paid else 'blocked', repaid=paid, debt_after=user.debt,
            cash_after=user.cash, economic_version_after=user.economic_version,
            blocked_reason=None if cash_recovers else 'no_executable_group')
        public_event(session, user, run, pre, product=None, mode=mode, sold=0,
                     proceeds=ZERO, repaid=paid, source=source)
        # A paused/unquotable holding can recover later. Keep its run active:
        # otherwise resumption between maintenance and initial loses hysteresis.
        if cash_recovers or not after_cash.groups:
            await close_run(session, run=run,
                status='recovered' if cash_recovers else 'insolvent',
                now=datetime.now(timezone.utc))
        return run, pre, None, mode, 'triggered' if paid else 'blocked'
    return run, pre, target, mode, 'sell'


async def finish_locked(session, user, run, rate):
    await session.flush()
    post = await value_user_detailed(session, user.id, daily_rate=rate)
    if get_flags().thresholds.recovered(post.liquidation_equity, post.debt_effective):
        await close_run(session, run=run, status='recovered', now=datetime.now(timezone.utc))
    elif not any(g.executable and g.value > 0 for g in post.groups):
        if post.liquidation_equity <= 0:
            user.credit_frozen = True
            bump_economic_version(user)
        if post.groups:
            run.last_blocked_reason = 'no_executable_group'
            run.updated_at = datetime.now(timezone.utc)
        else:
            await close_run(session, run=run, status='insolvent',
                            now=datetime.now(timezone.utc))


async def execute_user(user_id, *, rate, pct, source):
    from app.services.fx.trading import execute_liquidation_sell_in_session
    from app.services.market_writer import WRITER
    from app.services.writer_ops import LiquidateGroupCmd
    for _ in range(get_flags().credit_risk_retry_limit + 1):
        async with async_session_maker() as session:
            deps = await discover_dependencies(session, user_id)
            preliminary = await value_user_detailed(session, user_id, daily_rate=rate)
            deferred = _defer_short(preliminary)
            guess, _ = (None, None) if deferred else choose_group(preliminary, deps, pct)
        command = None
        publication = None
        async with GATES.hold(exclusive=[guess] if guess else (), shared=deps.groups):
            async with async_session_maker() as session:
                async with session.begin():
                    # FX product rows precede User, even when the final target changes.
                    ids = [guess.group_id] if guess and guess.product == 'fx' else []
                    if ids:
                        await session.execute(select(FxPair).where(FxPair.id.in_(ids)).order_by(FxPair.id).with_for_update())
                    user = await lock_user(session, user_id)
                    if user.economic_version != deps.economic_version:
                        continue
                    fresh = await discover_dependencies(session, user_id)
                    if set(fresh.groups) != set(deps.groups):
                        continue
                    current = await value_user_detailed(session, user_id, daily_rate=rate)
                    if _defer_short(current):
                        # 未知 K/已知空头：不选资产、不卖、不还金债，只维护 run。
                        run, pre, target, mode, status = await prepare_locked(session, user, fresh,
                            rate=rate, pct=pct, source=source, pre=current)
                        return status
                    if deferred:
                        # 空头在锁外发现后消失：重新发现，不沿用旧的资产选择。
                        continue
                    current_target, _ = choose_group(current, fresh, pct)
                    if current_target != guess:
                        continue
                    run, pre, target, mode, status = await prepare_locked(session, user, fresh,
                        rate=rate, pct=pct, source=source, pre=current)
                    if status != 'sell':
                        return status
                    if target.product == 'lmsr':
                        command = LiquidateGroupCmd(market_id=target.group_id, user_id=user_id,
                            run_id=run.id, round_no=run.next_round, mode=mode, partial_pct=pct,
                            fee_rate=fresh.lmsr_fee_rate, daily_rate=rate, trigger_source=source,
                            revalidate_account=True)
                    else:
                        round_no = run.next_round
                        if await find_action_for_round(session, run=run, round_no=round_no):
                            return 'replayed'
                        result = await execute_liquidation_sell_in_session(session, user_id=user_id,
                            pair_id=target.group_id, run_id=run.id, round_no=round_no, mode=mode, partial_pct=pct)
                        if result.blocked_reason or result.replay:
                            raise RuntimeError('FX execution diverged from locked preparation')
                        paid = await repay(session, user, rate, source)
                        q = result.quote
                        await record_action(session, run=run, round_no=round_no, kind='sell_group',
                            product='fx', group_id=target.group_id, mode=mode,
                            proceeds=q.gold_out, fee=q.fee_foreign, fee_currency='foreign', repaid=paid,
                            debt_after=user.debt, cash_after=user.cash, economic_version_after=user.economic_version,
                            executed={'foreign_in': str(q.foreign_in), 'sold_count': 1})
                        public_event(session, user, run, pre, product='fx', mode=mode, sold=1,
                                     proceeds=q.gold_out, repaid=paid, source=source)
                        await finish_locked(session, user, run, rate)
                        publication = (result.public.pair_id, result.public.post_price, result.public.id)
        if publication is not None:
            from app.services.fx.publisher import enqueue_publication
            enqueue_publication(pair_id=publication[0], post_price=publication[1], trade_id=publication[2])
            return 'triggered'
        # Absolutely no gates, session, or row locks survive queue submission.
        if command:
            if not WRITER.enabled:
                raise RuntimeError('unified LMSR liquidation requires market writer')
            result = await WRITER.submit(command)
            if result.get('blocked_reason') == 'selection_changed':
                continue
            return 'triggered' if result.get('sold_count') or result.get('repaid') else 'recovered'
    return 'skipped'
