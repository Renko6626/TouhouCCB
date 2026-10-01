"""One scheduled liquidation batch. Queue/gate waits never retain DB resources.

WP4c 在 WP4a 的候选发现/WP4b 的预算受限回补内核之上补齐已知完整估值的资金
顺序（spec §8.1）：

1. 新 run 按共享维持门槛触发，活跃 run 只在共享初始门槛恢复；有空头时绝不
   使用只看金债的 ``debt==0`` 快路径伪恢复（spec §6.1）。
2. 只要还有市场允许、补足现金即可成交的空头，暂缓自动还金债，把未锁现金
   留作回补预算；否则沿用旧的未锁现金还债并重估。
3. 正资产组按整组净回收 A、空头组按整组回补成本 K，按绝对值降序、同额按
   ``(product,id)`` 检查实际可执行条件，选第一个可成交组；现金不足/输出为
   零/停牌/未知的空头跳过，让后面的资产仍可卖出。
4. 每账户每轮最多一个资产组动作；``(run_id,round_no)`` 幂等，回补内核在同一
   事务内记录 ``cover_group``，提交后才发布 FX 价格。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_CEILING, Decimal

from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.base import LiquidationEvent, User
from app.models.credit import LiquidationRun
from app.models.fx import FxPair, FxShortPosition, FxTreasury
from app.services import audit_service, loan_service
from app.services.credit.cash import CashInvariantError
from app.services.credit.flags import get_flags
from app.services.credit.fx_quote import BLOCKED_SHORT_QUOTE_FAILED, quote_fx_group
from app.services.credit.gates import GATES
from app.services.credit.keys import GroupKey
from app.services.credit.lmsr_quote import quote_lmsr_group
from app.services.credit.ownership import OWNERSHIP
from app.services.credit.risk import discover_dependencies
from app.services.credit.runs import (
    close_run,
    find_action_for_round,
    find_latest_action,
    get_or_create_active_run,
    record_action,
)
from app.services.credit.valuation import (
    RISK_STATUS_OK,
    AccountValuation,
    value_user_detailed,
)
from app.services.credit.version import bump_economic_version
from app.services.fx.amm import _STORAGE_MAX, quote_buy, quote_buy_exact_out
from app.services.fx.shorts import (
    ShortRejected,
    ShortRetryCredit,
    pending_short_debt,
)
from app.services.market_locks import lock_user

ZERO = Decimal('0')
Q6 = Decimal('0.000001')
#: WP4a 遗留的通用空头延迟原因（仅未知 K/数据错误仍会用到）。
SHORT_DEFERRED_REASON = 'short_cover_deferred'
NO_EXECUTABLE_GROUP = 'no_executable_group'


def _has_foreign_obligation(pre: AccountValuation) -> bool:
    """估值里存在空头组 ⇔ 该账户有正的外币本金/利息（spec §8.2）。"""
    return any(group.role == 'short_cover' for group in pre.groups)


def _foreign_resources_exhausted(pre: AccountValuation) -> bool:
    """Only complete, unblocked obligations with no remaining resources are bad debt.

    Keep even zero-output asset holdings for later repair/resumption; current
    batch output or budget does not establish permanent exhaustion.
    """
    return (pre.risk_status == RISK_STATUS_OK
            and pre.liquidation_equity is not None
            and pre.short_cover_cost is not None
            and pre.cash == ZERO
            and _has_foreign_obligation(pre)
            and all(g.role == 'short_cover' and g.executable
                    and g.blocked_reason is None for g in pre.groups))


def _waiting_reason(pre: AccountValuation) -> str:
    return pre.blocked_reason or ','.join(sorted({
        g.blocked_reason for g in pre.groups if g.blocked_reason
    })) or NO_EXECUTABLE_GROUP


def _freeze_foreign_debtor(user):
    if not user.credit_frozen:
        user.credit_frozen = True
        bump_economic_version(user)


async def _close_foreign_insolvent(session, user, run):
    _freeze_foreign_debtor(user)
    run.last_blocked_reason = NO_EXECUTABLE_GROUP
    await close_run(session, run=run, status='insolvent', now=datetime.now(timezone.utc))


def _positive_assets(pre: AccountValuation) -> Decimal:
    return sum(
        (group.value for group in pre.groups
         if group.role == 'asset_sale' and group.value is not None),
        ZERO,
    )


def _triggered(pre: AccountValuation, thresholds) -> bool:
    """已知完整估值才比较：共享 E/B 门槛，K 取自权威估值（spec §6.1/§8.1）。"""
    return thresholds.triggered_basis(
        equity=pre.liquidation_equity,
        debt=pre.debt_effective,
        positive_assets=_positive_assets(pre),
        short_cover=pre.short_cover_cost,
    )


def _recovered(thresholds, pre: AccountValuation, has_short: bool, *, debt=None) -> bool:
    """共享恢复判定（spec §6.1）；无空头账户保持旧 ``recovered`` 语义。

    ``has_short`` 时比较 ``E >= I*B``（等价 ``L(L-1)E >= W``），只有金债和
    外币义务都为零才允许快路径恢复。无空头时委托旧 ``E >= r_initial*D``，
    与 WP2 的等价性证明一致。
    """
    debt_value = pre.debt_effective if debt is None else debt
    if not has_short and debt_value <= 0:
        return True
    if pre.risk_status != RISK_STATUS_OK or pre.liquidation_equity is None:
        return False
    if not has_short:
        return thresholds.recovered(pre.liquidation_equity, debt_value)
    cover = pre.short_cover_cost
    equity = pre.liquidation_equity
    if equity is None or cover is None:
        return False
    return thresholds.admits(
        equity=equity, debt=debt_value,
        positive_assets=_positive_assets(pre), short_cover=cover,
    )


async def _seed_run_snapshot(session, run, pre):
    """run 起点快照；未知 E/B 时保持 None，绝不写成 0。"""
    run.pre_cash = pre.cash
    run.pre_debt = pre.debt_effective
    run.pre_liquidation_equity = pre.liquidation_equity
    run.pre_risk_basis = pre.risk_basis
    run.pre_equity_to_risk_basis = (
        pre.liquidation_equity / pre.risk_basis
        if pre.liquidation_equity is not None and pre.risk_basis is not None
        and pre.risk_basis > ZERO else None
    )
    # The User lock and full dependency GATES already guard these rows. This
    # read adds no row locks and preserves pair → User → short/treasury order.
    positions = (await session.execute(select(FxShortPosition).where(
        FxShortPosition.user_id == run.user_id).order_by(FxShortPosition.pair_id)
    )).scalars().all()
    run.pre_short_positions = {
        str(position.pair_id): {
            'principal_foreign': str(position.principal_foreign),
            'interest_foreign': str(position.interest_foreign),
            'restricted_gold': str(position.restricted_gold),
            'proceeds_basis_gold': str(position.proceeds_basis_gold),
            'interest_last_accrued_at': (
                position.interest_last_accrued_at.isoformat()
                if position.interest_last_accrued_at is not None else None),
        } for position in positions
    }


def _blocked_short_group(pre: AccountValuation, reason):
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


async def repay(session, user, rate, source, *, cash_cap=None):
    before = user.debt
    before_clock = user.debt_last_accrued_at
    now = loan_service._compat_now(user)
    loan_service.accrue_interest(user, rate, now)
    spendable = user.cash if cash_cap is None else min(user.cash, cash_cap)
    amount = min(max(spendable, ZERO), user.debt)
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


async def accrue_gold_only(session, user, rate, source):
    """结息但不还债：有条件留存现金时仍推进金债时点（spec §8.1 第 1 步）。"""
    before = user.debt
    before_clock = user.debt_last_accrued_at
    now = loan_service._compat_now(user)
    loan_service.accrue_interest(user, rate, now)
    if user.debt == before and user.debt_last_accrued_at == before_clock:
        return ZERO
    bump_economic_version(user)
    audit_service.record(session, "interest_accrual", user_id=user.id,
        payload={"debt_before": before, "debt_after": user.debt,
                 "interest": user.debt-before, "daily_rate": rate, "source": source},
        user_after=audit_service.user_snapshot(user))
    return ZERO


def post_sale_free_cash(user, pre) -> Decimal:
    """卖出正资产后可用于还金债的权威现金 = 当前 ``cash − Σ锁金``。

    卖出一组正资产只会增加现金，不改变任何空头的 ``restricted_gold``（本组是
    资产组，不是空头对），所以锁内 ``pre.restricted_cash`` 仍是权威锁金快照。
    若仍拿卖出**前**的 ``pre.available_cash`` 当还款上限，刚到手的净回款会被
    误判为不可用，金债少还、run 无谓保持 active（spec §8.1 第 1/4 步“否则可还
    自由现金”）。锁金高于现金属于违反 ``0<=ΣS<=C`` 的不变量：必须失败回滚，
    绝不把差额当可用现金花掉。
    """
    cash = Decimal(user.cash)
    restricted = Decimal(pre.restricted_cash)
    if not cash.is_finite() or not restricted.is_finite() or cash < restricted:
        raise CashInvariantError(
            f"restricted cash exceeds total cash after sale: cash={cash}, restricted={restricted}")
    return cash - restricted


def public_event(session, user, run, pre, *, product, mode, sold, proceeds, repaid, source,
                 foreign_repaid=ZERO):
    """公开强平事件；未知 E 时金额字段保持 ``None``，不写 0/Infinity。"""
    if not sold and repaid <= 0 and foreign_repaid <= 0:
        return
    now = datetime.now(timezone.utc)
    equity = pre.liquidation_equity
    holdings_value = None if equity is None else equity + pre.debt_effective - pre.cash
    margin_ratio = (
        None if equity is None or not pre.debt_effective else equity / pre.debt_effective
    )
    user.last_liquidated_at = now
    session.add(LiquidationEvent(user_id=user.id, run_id=run.id, product=product,
        triggered_at=now, pre_cash=pre.cash, pre_debt=pre.debt_effective,
        pre_holdings_value=holdings_value,
        pre_net_worth=equity,
        pre_margin_ratio=margin_ratio,
        sold_positions_count=sold, total_proceeds=proceeds, repaid_amount=repaid,
        remaining_debt=user.debt, post_cash=user.cash, trigger_source=source, mode=mode))


# ── WP4c 组选择：正资产按 A、空头按 K，绝对值降序，同额 (product,id) ──────────

@dataclass(frozen=True)
class Choice:
    """本轮第一个实际可执行的组动作（空头回补或正资产卖出）。"""

    target: GroupKey
    role: str                 # "asset_sale" | "short_cover"
    mode: str                 # "partial" | "full"
    planned: Decimal | None = None   # 空头回补计划量（封顶后）
    budget: Decimal | None = None    # 空头回补硬预算 = 未锁现金 + 本仓锁金


@dataclass
class LockedPlan:
    """``prepare_locked`` 的锁内结果；调用方据此执行卖出/回补/还债。"""

    run: LiquidationRun | None
    pre: AccountValuation
    target: GroupKey | None
    role: str | None
    mode: str | None
    planned: Decimal | None
    budget: Decimal | None
    status: str               # replayed|blocked|recovered|triggered|sell|cover
    repaid: Decimal = ZERO
    defer_repay: bool = False
    blocked_reason: str | None = None


def _ceil6(value: Decimal) -> Decimal:
    return value.quantize(Q6, rounding=ROUND_CEILING)


def _short_cover_plan(group, deps, free_cash, *, mode, pct, daily_rate, now):
    """空头的纯前置检查：返回 ``(planned, budget)`` 或 ``None``。

    ``planned`` 是 ``ceil_6(Q_effective*partial_pct)`` 封顶 Q_effective；E<=0
    时计划全仓。``budget = 未锁现金 + 本仓锁金``（绝不借新金债、绝不动别的
    空头锁金，spec §8.1 第 5 步）。任何无法形成正数量成交的情形返回 ``None``，
    让调用方跳到后续组。
    """
    snap = deps.snapshots.get(group.key)
    if snap is None or snap.short_debt is None or snap.short_pair is None:
        return None
    try:
        q_effective = pending_short_debt(snap.short_debt, daily_rate, now).quantize(Q6)
    except ShortRejected:
        return None
    if q_effective <= 0:
        return None
    pair = snap.short_pair
    gold_reserve = Decimal(pair.gold_reserve)
    foreign_reserve = Decimal(pair.foreign_reserve)
    if (not gold_reserve.is_finite() or not foreign_reserve.is_finite()
            or gold_reserve <= 0 or foreign_reserve <= 0):
        return None
    # Q>=F 无法完整买回（spec §8.2）：不把反复部分回补当恢复办法。
    if q_effective >= foreign_reserve:
        return None
    planned = (q_effective if mode == 'full'
               else min(_ceil6(q_effective * pct), q_effective))
    if planned <= 0:
        return None
    requested = min(planned, q_effective)
    own_lock = Decimal(snap.short_debt.restricted_gold)
    budget = (free_cash + own_lock).quantize(Q6)
    if budget <= 0:
        return None
    fee_rate = Decimal(pair.buy_fee_rate)
    try:
        exact = quote_buy_exact_out(requested, gold_reserve, foreign_reserve, fee_rate)
    except (ValueError, ArithmeticError):
        exact = None
    if exact is not None and exact.input_amount <= budget:
        return planned, budget
    try:
        exact_in = quote_buy(budget, gold_reserve, foreign_reserve, fee_rate)
    except (ValueError, ArithmeticError):
        return None
    if exact_in.output_amount <= 0 or exact_in.output_amount > requested:
        return None
    return planned, budget


def _pending_cover_exists(pre, deps, *, mode, pct, daily_rate, now) -> bool:
    """暂缓还债的条件（spec §8.1 第 1 步）：忽略当前预算，只看补齐现金后能否成交。

    真实故障：把“当前买不起”当成“不需要留钱”，会把卖资产所得立刻还金债，
    下一轮空头仍无预算；spec §8.1 的零预算最大空头轮换正是要避免这一点。
    """
    for group in pre.groups:
        if (group.role != 'short_cover' or not group.executable
                or group.value is None or group.value <= 0):
            continue
        snap = deps.snapshots.get(group.key)
        if snap is None or snap.short_debt is None or snap.short_pair is None:
            continue
        try:
            q_effective = pending_short_debt(snap.short_debt, daily_rate, now).quantize(Q6)
        except ShortRejected:
            continue
        pair = snap.short_pair
        foreign_reserve = Decimal(pair.foreign_reserve)
        if q_effective <= 0 or foreign_reserve <= 0 or q_effective >= foreign_reserve:
            continue
        planned = (q_effective if mode == 'full'
                   else min(_ceil6(q_effective * pct), q_effective))
        if planned <= 0:
            continue
        try:
            exact = quote_buy_exact_out(
                min(planned, q_effective), Decimal(pair.gold_reserve),
                foreign_reserve, Decimal(pair.buy_fee_rate))
        except (ValueError, ArithmeticError):
            continue
        if exact.input_amount > 0 and exact.output_amount > 0:
            return True
    return False


def _asset_executable(group, deps, *, mode, pct) -> bool:
    snap = deps.snapshots.get(group.key)
    if snap is None:
        return False
    holdings = deps.holdings.get(group.key, {})
    if group.key.product == 'fx':
        if snap.pair is None:
            return False
        quote = quote_fx_group(snap.pair, foreign_amount=sum(holdings.values(), ZERO),
                               mode=mode, partial_pct=pct)
        return quote.blocked_reason is None and quote.gold_out > 0
    quote = quote_lmsr_group(snap.outcomes, holdings, market_id=group.key.group_id,
                             b=snap.b, fee_rate=snap.fee_rate, mode=mode, partial_pct=pct)
    return quote.blocked_reason is None and quote.net > 0 and bool(quote.legs)


async def _cover_treasury_balances(session, pre):
    pair_ids = [g.key.group_id for g in pre.groups if g.role == 'short_cover']
    if not pair_ids:
        return {}
    # Columns bypass the ORM identity cache: locked revalidation must see current
    # inventory. Discovery is optimistic; execute_user repeats this read under
    # all dependency GATES before checking the selected exclusive target. This
    # read takes no additional pair gate or row lock.
    rows = (await session.execute(select(
        FxTreasury.pair_id, FxTreasury.gold_balance, FxTreasury.foreign_balance,
    ).where(FxTreasury.pair_id.in_(pair_ids)))).all()
    treasuries = {pid: (Decimal(gold), Decimal(foreign)) for pid, gold, foreign in rows}
    return treasuries


def _cover_quote_storable(pair, plan, treasury):
    """Preflight the actual budget quote and both physical treasury legs.

    This supplements the kernel's rollback-required final bound checks so an
    illegal high-ranked group can be skipped before interest settlement.
    """
    try:
        try:
            quote = quote_buy_exact_out(plan[0], pair.gold_reserve,
                                        pair.foreign_reserve, pair.buy_fee_rate)
        except (TypeError, ValueError, ArithmeticError):
            quote = None
        if quote is None or quote.input_amount > plan[1]:
            quote = quote_buy(plan[1], pair.gold_reserve, pair.foreign_reserve,
                              pair.buy_fee_rate)
        # Only a genuinely absent row starts at zero, matching the kernel's
        # treasury creation path. quote_buy does not enforce Numeric bounds.
        treasury_gold, treasury_foreign = treasury
        persisted = (quote.input_amount, quote.fee_amount, quote.output_amount,
                     quote.post_gold_reserve, quote.post_foreign_reserve,
                     quote.post_price, treasury_gold, treasury_foreign,
                     treasury_gold + quote.fee_amount,
                     treasury_foreign + quote.output_amount)
        return not any(not v.is_finite() or v < ZERO or v > _STORAGE_MAX for v in persisted)
    except (TypeError, ValueError, ArithmeticError):
        return False


async def _choose_unknown_overflow_cover(session, pre, deps, *, pct, daily_rate, now):
    """§8.2: only a trustworthy full-quote storage overflow permits spending.

    The AMM distinguishes input precision/range and fee errors from these two
    output/post-state range errors. Do not turn every ``short_quote_failed``
    (which also includes invalid fees) into permission to liquidate.
    """
    if (pre.available_cash is None or not pre.available_cash.is_finite()
            or pre.available_cash < ZERO or not pre.debt_effective.is_finite()
            or pre.debt_effective < ZERO):
        return None
    treasuries = await _cover_treasury_balances(session, pre)
    for group in sorted(pre.groups, key=lambda g: (g.key.product, g.key.group_id)):
        if (group.role != 'short_cover'
                or group.blocked_reason != BLOCKED_SHORT_QUOTE_FAILED):
            continue
        snap = deps.snapshots.get(group.key)
        if snap is None or snap.short_pair is None or snap.short_debt is None:
            continue
        pair = snap.short_pair
        status = str(pair.status or '').strip().lower()
        if status != 'trading' and not (status == 'paused' and pair.reduce_only):
            continue
        try:
            q = pending_short_debt(snap.short_debt, daily_rate, now).quantize(Q6)
            # This validates reserves, debt precision and fees before identifying
            # overflow; Q>=F and invalid data remain mutation-free blocks.
            quote_buy_exact_out(q, pair.gold_reserve, pair.foreign_reserve,
                                pair.buy_fee_rate)
        except ValueError as exc:
            if str(exc) not in ('quote exceeds storage range',
                                'post price exceeds storage range'):
                continue
        except (TypeError, ArithmeticError):
            continue
        else:
            continue
        plan = _short_cover_plan(group, deps, pre.available_cash, mode='partial',
                                 pct=pct, daily_rate=daily_rate, now=now)
        if plan is None:
            continue
        if not _cover_quote_storable(pair, plan, treasuries.get(group.key.group_id, (ZERO, ZERO))):
            continue
        return Choice(group.key, 'short_cover', 'partial', plan[0], plan[1])
    return None


async def choose_liquidation_action(session, pre, deps, *, pct, daily_rate, now):
    """按完整性分支选择可执行组；批量读真实 treasury，始终不写库。"""
    if pre.risk_status != RISK_STATUS_OK:
        return await _choose_unknown_overflow_cover(
            session, pre, deps, pct=pct, daily_rate=daily_rate, now=now)
    if pre.liquidation_equity is None or pre.available_cash is None:
        return None
    mode = 'full' if pre.liquidation_equity <= 0 else 'partial'
    treasuries = await _cover_treasury_balances(session, pre)
    for group in pre.groups:
        if group.value is None or group.value <= 0 or not group.executable:
            continue
        if group.role == 'short_cover':
            plan = _short_cover_plan(group, deps, pre.available_cash, mode=mode,
                                     pct=pct, daily_rate=daily_rate, now=now)
            if plan is None:
                continue
            pair = deps.snapshots[group.key].short_pair
            if not _cover_quote_storable(
                    pair, plan, treasuries.get(group.key.group_id, (ZERO, ZERO))):
                continue
            return Choice(target=group.key, role='short_cover', mode=mode,
                          planned=plan[0], budget=plan[1])
        if _asset_executable(group, deps, mode=mode, pct=pct):
            return Choice(target=group.key, role='asset_sale', mode=mode)
    return None


def _same_choice(guess, current) -> bool:
    """锁外猜测与锁内选择是否指向同一组同一目的（数量可在锁内重算）。"""
    if guess is None or current is None:
        return guess is current
    return guess.target == current.target and guess.role == current.role


async def prepare_locked(session, user, deps, *, rate, pct, source,
                         run_id=None, round_no=None, pre=None, choice=None):
    """Fresh, fully gated account check.

    Returns a :class:`LockedPlan`: ``status`` is ``sell`` (an asset group was
    selected), ``cover`` (a short-cover group was selected), ``triggered``
    (cash-only repayment ran), ``blocked``, ``recovered`` or ``replayed``.
    Cash is repaid here only on the cash-only path; a sale/cover executor owns
    the atomic action and, when ``defer_repay`` is set, retains the proceeds.
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
            return LockedPlan(expected, pre, None, None, None, None, None, 'replayed')
        if run is None or run.id != run_id or run.next_round != round_no:
            raise ValueError('stale liquidation round')
    thresholds = get_flags().thresholds

    # 未知 K 不比较门槛、不还金债/卖资产；仅完整报价溢出可固定比例回补。
    if pre.risk_status != RISK_STATUS_OK:
        created = run is None
        if run is None:
            run = await get_or_create_active_run(session, user_id=user.id,
                trigger_source=source, now=datetime.now(timezone.utc))
        if created:
            await _seed_run_snapshot(session, run, pre)
            await session.flush()
        overflow_choice = await _choose_unknown_overflow_cover(
            session, pre, deps, pct=pct, daily_rate=rate, now=datetime.now(timezone.utc))
        if choice is not None and not _same_choice(choice, overflow_choice):
            # Interest may move a boundary between revalidation and planning;
            # never switch the exclusive target while holding the old GATE.
            raise ShortRetryCredit()
        choice = overflow_choice
        if choice is not None:
            # Unknown E never authorizes asset sales, gold repayment or a full
            # batch. The target was selected and revalidated under its GATE.
            return LockedPlan(run, pre, choice.target, 'short_cover', 'partial',
                              choice.planned, choice.budget, 'cover', defer_repay=True)
        await _record_blocked_once(session, user, run,
            group=_blocked_short_group(pre, pre.blocked_reason),
            reason=pre.blocked_reason or 'risk_unquotable')
        return LockedPlan(run, pre, None, None, None, None, None, 'blocked',
                          blocked_reason=pre.blocked_reason or 'risk_unquotable')

    has_short = _has_foreign_obligation(pre)

    if run is None and not _triggered(pre, thresholds):
        return LockedPlan(None, pre, None, None, None, None, None, 'recovered')
    if run is None and _foreign_resources_exhausted(pre):
        # Terminal debt remains a scan candidate. Reuse its terminal outcome
        # while exhausted, but reconsider any later cash or asset resource.
        latest = (await session.execute(select(LiquidationRun).where(
            LiquidationRun.user_id == user.id).order_by(LiquidationRun.id.desc())
            .limit(1).with_for_update())).scalars().first()
        if latest is not None and latest.status == 'insolvent':
            return LockedPlan(latest, pre, None, None, None, None, None, 'blocked',
                              blocked_reason=latest.last_blocked_reason)
    if run is None:
        run = await get_or_create_active_run(session, user_id=user.id, trigger_source=source,
                                             now=datetime.now(timezone.utc))
        await _seed_run_snapshot(session, run, pre)
        await session.flush()

    # 活跃 run 只在共享初始门槛恢复；有空头时绝不因 D==0 快路径伪恢复。
    if _recovered(thresholds, pre, has_short):
        await close_run(session, run=run, status='recovered', now=datetime.now(timezone.utc))
        return LockedPlan(run, pre, None, None, None, None, None, 'recovered')

    if _foreign_resources_exhausted(pre):
        _freeze_foreign_debtor(user)
        await _record_blocked_once(session, user, run, group=None, reason=NO_EXECUTABLE_GROUP)
        await _close_foreign_insolvent(session, user, run)
        return LockedPlan(run, pre, None, None, None, None, None, 'blocked',
                          blocked_reason=NO_EXECUTABLE_GROUP)

    now = datetime.now(timezone.utc)
    mode = 'full' if pre.liquidation_equity <= 0 else 'partial'
    defer_repay = _pending_cover_exists(pre, deps, mode=mode, pct=pct,
                                        daily_rate=rate, now=now)
    if choice is None:
        choice = await choose_liquidation_action(session, pre, deps, pct=pct, daily_rate=rate, now=now)

    # 有待回补空头：绝不自动还金债，现金留作回补预算，直接执行排序第一的组。
    if choice is not None and defer_repay:
        await session.flush()
        return LockedPlan(run, pre, choice.target,
                          'short_cover' if choice.role == 'short_cover' else 'asset_sale',
                          choice.mode, choice.planned, choice.budget,
                          'cover' if choice.role == 'short_cover' else 'sell',
                          defer_repay=True)

    # 只有未锁现金可还金债；有空头时绝不把空头 S 花在金债上（spec §8.1 第 1 步）。
    free_cash = pre.available_cash if has_short else pre.cash
    free_cash = ZERO if free_cash is None else free_cash
    cash_repay = min(max(free_cash, ZERO), pre.debt_effective)
    cash_recovers = _recovered(thresholds, pre, has_short, debt=pre.debt_effective - cash_repay)
    if (not defer_repay) and (cash_recovers or choice is None):
        paid = await repay(session, user, rate, source,
                           cash_cap=(None if not has_short else pre.available_cash))
        await session.flush()
        after = await value_user_detailed(session, user.id, daily_rate=rate)
        cash_recovers = _recovered(thresholds, after, has_short)
        if (not has_short and not cash_recovers
                and pre.liquidation_equity is not None and pre.liquidation_equity <= 0):
            user.credit_frozen = True
            bump_economic_version(user)
        action = await record_action(session, run=run, round_no=run.next_round,
            kind='repay_cash' if paid else 'blocked', repaid=paid, debt_after=user.debt,
            cash_after=user.cash, economic_version_after=user.economic_version,
            blocked_reason=None if cash_recovers else (
                _waiting_reason(after) if has_short else NO_EXECUTABLE_GROUP))
        public_event(session, user, run, pre, product=None, mode=mode, sold=0,
                     proceeds=ZERO, repaid=paid, source=source)
        # A paused/unquotable holding can recover later. Keep its run active:
        # otherwise resumption between maintenance and initial loses hysteresis.
        if has_short and _foreign_resources_exhausted(after):
            await _close_foreign_insolvent(session, user, run)
        elif cash_recovers or not after.groups:
            await close_run(session, run=run,
                status='recovered' if cash_recovers else 'insolvent',
                now=datetime.now(timezone.utc))
        action.economic_version_after = user.economic_version
        return LockedPlan(run, pre, None, None, mode, None, None,
                          'triggered' if paid else 'blocked', repaid=paid,
                          blocked_reason=None if cash_recovers else (
                              _waiting_reason(after) if has_short else NO_EXECUTABLE_GROUP))

    if choice is not None:
        await session.flush()
        return LockedPlan(run, pre, choice.target,
                          'short_cover' if choice.role == 'short_cover' else 'asset_sale',
                          choice.mode, choice.planned, choice.budget,
                          'cover' if choice.role == 'short_cover' else 'sell',
                          defer_repay=defer_repay)

    # 没有可执行组但仍有待回补空头（只可能是预算恰好为零，无现金可还）：保留 run。
    reason = _waiting_reason(pre)
    await _record_blocked_once(session, user, run, group=None, reason=reason)
    return LockedPlan(run, pre, None, None, mode, None, None, 'blocked', defer_repay=True,
                      blocked_reason=reason)


async def finish_locked(session, user, run, rate):
    await session.flush()
    post = await value_user_detailed(session, user.id, daily_rate=rate)
    thresholds = get_flags().thresholds
    has_short = _has_foreign_obligation(post)
    if _recovered(thresholds, post, has_short):
        await close_run(session, run=run, status='recovered', now=datetime.now(timezone.utc))
        return
    if post.risk_status != RISK_STATUS_OK:
        run.last_blocked_reason = post.blocked_reason or 'risk_unquotable'
        run.updated_at = datetime.now(timezone.utc)
        return
    if has_short and _foreign_resources_exhausted(post):
        await _close_foreign_insolvent(session, user, run)
        return
    executable = any(g.executable and g.value is not None and g.value > 0
                     for g in post.groups)
    if executable:
        return
    if has_short:
        run.last_blocked_reason = _waiting_reason(post)
        run.updated_at = datetime.now(timezone.utc)
        return
    if post.liquidation_equity is not None and post.liquidation_equity <= 0:
        user.credit_frozen = True
        bump_economic_version(user)
    if post.groups:
        run.last_blocked_reason = NO_EXECUTABLE_GROUP
        run.updated_at = datetime.now(timezone.utc)
    else:
        await close_run(session, run=run, status='insolvent',
                        now=datetime.now(timezone.utc))


async def _execute_cover_locked(session, user, plan, fresh, *, rate, source):
    """执行一个已选择的空头回补组；返回 ``(publication, status)``。

    ``plan.target`` 的独占 GATE 与完整依赖 GATE 已由调用方持有，事务由调用方
    拥有。正常 blocked 返回是 mutation-free 的，可以落盘并提交；任何
    :class:`ShortRetryCredit`（含 :class:`ShortPostSettlementMismatch`）都
    向上抛出，由调用方回滚整个事务后再重试/跳过，绝不写成可提交的 blocked。
    """
    from app.services.fx.shorts import execute_liquidation_cover_in_session

    run = plan.run
    round_no = run.next_round
    if await find_action_for_round(session, run=run, round_no=round_no):
        return None, 'replayed'
    execution = await execute_liquidation_cover_in_session(
        session, user_id=user.id, pair_id=plan.target.group_id, run_id=run.id,
        round_no=round_no, planned_amount=plan.planned, max_gold_budget=plan.budget,
        credit_deps=fresh,
    )
    if execution.blocked_reason:
        blocked_group = next(
            (g for g in plan.pre.groups if g.key == plan.target), None)
        await _record_blocked_once(session, user, run, group=blocked_group,
                                   reason=execution.blocked_reason)
        return None, 'blocked'
    action = await record_action(
        session, run=run, round_no=round_no, kind='cover_group',
        product='fx', group_id=plan.target.group_id, mode=plan.mode,
        requested={'planned_amount': str(plan.planned), 'max_gold_budget': str(plan.budget)},
        executed={
            'limited_by_cash': bool(execution.limited_by_cash),
            'full_cover': bool(execution.full_cover),
            'repaid_foreign': str(execution.repaid_foreign),
            'paid_gold': str(execution.paid_gold),
            'released_lock': str(execution.released_lock),
        },
        gold_spent=execution.paid_gold, foreign_repaid=execution.repaid_foreign,
        short_after={
            'principal_foreign': str(execution.principal_foreign_after),
            'interest_foreign': str(execution.interest_foreign_after),
            'restricted_gold': str(execution.restricted_gold_after),
            'proceeds_basis_gold': str(execution.proceeds_basis_gold_after),
        },
        fee=execution.fee_gold, fee_currency='gold', repaid=ZERO,
        debt_after=user.debt, cash_after=user.cash,
        economic_version_after=user.economic_version,
    )
    public_event(session, user, run, plan.pre, product='fx', mode=plan.mode,
                 sold=0, proceeds=ZERO, repaid=ZERO, source=source,
                 foreign_repaid=execution.repaid_foreign)
    await finish_locked(session, user, run, rate)
    action.economic_version_after = user.economic_version
    return (plan.target.group_id, execution.post_price, execution.trade_id), 'triggered'


async def execute_user(user_id, *, rate, pct, source):
    from app.services.fx.trading import execute_liquidation_sell_in_session
    from app.services.market_writer import WRITER
    from app.services.writer_ops import LiquidateGroupCmd

    retries = get_flags().credit_risk_retry_limit + 1
    for _ in range(retries):
        async with async_session_maker() as session:
            deps = await discover_dependencies(session, user_id)
            preliminary = await value_user_detailed(session, user_id, daily_rate=rate)
            guess = await choose_liquidation_action(
                session, preliminary, deps, pct=pct, daily_rate=rate,
                now=datetime.now(timezone.utc))
        command = None
        publication = None
        try:
            async with GATES.hold(exclusive=[guess.target] if guess else (),
                                  shared=deps.groups):
                async with async_session_maker() as session:
                    async with session.begin():
                        # FX product rows precede User, for both sell and cover.
                        ids = ([guess.target.group_id]
                               if guess and guess.target.product == 'fx' else [])
                        if ids:
                            await session.execute(
                                select(FxPair).where(FxPair.id.in_(ids))
                                .order_by(FxPair.id).with_for_update())
                        user = await lock_user(session, user_id)
                        if user.economic_version != deps.economic_version:
                            continue
                        fresh = await discover_dependencies(session, user_id)
                        if set(fresh.groups) != set(deps.groups):
                            continue
                        current = await value_user_detailed(session, user_id, daily_rate=rate)
                        now = datetime.now(timezone.utc)
                        current_choice = await choose_liquidation_action(
                            session, current, fresh, pct=pct, daily_rate=rate, now=now)
                        if not _same_choice(guess, current_choice):
                            continue
                        plan = await prepare_locked(
                            session, user, fresh, rate=rate, pct=pct, source=source,
                            pre=current, choice=current_choice)

                        if plan.status == 'cover':
                            publication, status = await _execute_cover_locked(
                                session, user, plan, fresh, rate=rate, source=source)
                            if status != 'triggered':
                                return status
                        elif plan.status == 'sell' and plan.target.product == 'lmsr':
                            command = LiquidateGroupCmd(
                                market_id=plan.target.group_id, user_id=user_id,
                                run_id=plan.run.id, round_no=plan.run.next_round,
                                mode=plan.mode, partial_pct=pct,
                                fee_rate=fresh.lmsr_fee_rate, daily_rate=rate,
                                trigger_source=source, revalidate_account=True)
                        elif plan.status == 'sell':
                            round_no = plan.run.next_round
                            if await find_action_for_round(session, run=plan.run,
                                                           round_no=round_no):
                                return 'replayed'
                            result = await execute_liquidation_sell_in_session(
                                session, user_id=user_id, pair_id=plan.target.group_id,
                                run_id=plan.run.id, round_no=round_no,
                                mode=plan.mode, partial_pct=pct)
                            if result.blocked_reason or result.replay:
                                raise RuntimeError(
                                    'FX execution diverged from locked preparation')
                            if plan.defer_repay:
                                # Reserve every unit of the proceeds for next-round
                                # cover; only advance the gold interest clock.
                                repaid = ZERO
                                await accrue_gold_only(session, user, rate, source)
                            else:
                                # Cap at post-sale free cash, not the pre-sale
                                # snapshot: the net proceeds just increased cash.
                                cash_cap = (post_sale_free_cash(user, plan.pre)
                                            if _has_foreign_obligation(plan.pre) else None)
                                repaid = await repay(session, user, rate, source,
                                                     cash_cap=cash_cap)
                            quote = result.quote
                            action = await record_action(
                                session, run=plan.run, round_no=round_no, kind='sell_group',
                                product='fx', group_id=plan.target.group_id, mode=plan.mode,
                                proceeds=quote.gold_out, fee=quote.fee_foreign,
                                fee_currency='foreign', repaid=repaid,
                                debt_after=user.debt, cash_after=user.cash,
                                economic_version_after=user.economic_version,
                                executed={'foreign_in': str(quote.foreign_in),
                                          'sold_count': 1})
                            public_event(session, user, plan.run, plan.pre, product='fx',
                                         mode=plan.mode, sold=1, proceeds=quote.gold_out,
                                         repaid=repaid, source=source)
                            await finish_locked(session, user, plan.run, rate)
                            action.economic_version_after = user.economic_version
                            publication = (result.public.pair_id, result.public.post_price,
                                           result.public.id)
                        else:
                            return plan.status
        except ShortRetryCredit:
            # Covers: version/economic drift or a rollback-required post-settlement
            # mismatch. The whole transaction is already rolled back; rediscover.
            # Never persist a post-settlement failure as a committable blocked round.
            continue
        if publication is not None:
            # Forced sell/cover committed: hint the incremental market-data
            # runtime, then enqueue the bounded discardable publication.
            from app.services.fx.publisher import enqueue_publication
            from app.services.fx.trading import notify_market_data_committed
            notify_market_data_committed(int(publication[0]))
            enqueue_publication(pair_id=publication[0], post_price=publication[1],
                                trade_id=publication[2])
            return 'triggered'
        # Absolutely no gates, session, or row locks survive queue submission.
        if command:
            if not WRITER.enabled:
                raise RuntimeError('unified LMSR liquidation requires market writer')
            result = await WRITER.submit(command)
            if result.get('blocked_reason') == 'selection_changed':
                continue
            if result.get('blocked_reason'):
                return 'blocked'
            return ('triggered' if result.get('sold_count') or result.get('repaid')
                    else 'recovered')
    return 'skipped'
