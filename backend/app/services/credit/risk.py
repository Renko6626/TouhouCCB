"""Unified risk using a fresh own ledger and one immutable published price capture.

Execution callers hold User FOR UPDATE through commit, discover the full account
before constructing their post-state, and pass account_locked=True. Market prices
never require a foreign market lock, SQL fallback, or a writer request.
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Mapping, Optional, Sequence

from sqlalchemy import inspect as sa_inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import Outcome, Position, SiteConfig, User
from app.models.fx import FxShortPosition, FxWallet
from app.services.credit import flags as credit_flags
from app.services.credit.fx_quote import (
    BLOCKED_SHORT_QUOTE_FAILED,
    FxPairSnapshot,
    FxShortPairSnapshot,
    quote_fx_group,
    quote_fx_short_group,
)
from app.services.credit.keys import GroupKey
from app.services.credit.lmsr_quote import OutcomeSnapshot, quote_lmsr_group
from app.services.credit.thresholds import RiskThresholds
from app.services.credit.version import economic_version_of
from app.services.fx.shorts import pending_short_debt
from app.services.loan_service import pending_debt

logger = logging.getLogger("thccb.credit.risk")

ZERO = Decimal("0")
ONE = Decimal("1")
Q6 = Decimal("0.000001")

LOAN_DAILY_RATE_KEY = "loan_daily_rate"
SELL_FEE_RATE_KEY = "sell_fee_rate"

#: 版本化组值缓存上界（按 (user, version, symbol, symbol_version) 一条）。
DEFAULT_CACHE_SIZE = 4096

REASON_INSUFFICIENT_INITIAL_MARGIN = "insufficient_initial_margin"
REASON_CREDIT_FROZEN = "credit_frozen"
REASON_FROZEN_BY_OPERATOR = "frozen_by_operator"
REASON_VERSION_CONFLICT = "version_conflict"
#: 空头 pair 快照缺失（pair 被删/未迁移）：K 未知，绝不按 0 处理。
REASON_SHORT_SNAPSHOT_MISSING = "short_snapshot_missing"
#: 持久外币欠币无法解析/超出存储范围：K 未知，绝不按 0 处理。
REASON_INVALID_SHORT_DEBT = "invalid_short_debt"
#: 正资产组补不到快照（品种被删/未迁移）：A 无法完整估值，E/B 未知。
REASON_INCOMPLETE_ASSET_VALUATION = "incomplete_asset_valuation"


# ────────────────────────────── 数据结构 ──────────────────────────────

@dataclass(frozen=True)
class ShortDebtSnapshot:
    """持久化的空头欠币（本金/利息/时点），用于按 T 计息。

    只保存**数据库权威值**；含息数量在 check 内按统一 ``now`` 用
    ``fx.shorts.pending_short_debt`` 计算，从不落库（spec §4/§11）。
    """

    principal_foreign: Decimal
    interest_foreign: Decimal
    interest_last_accrued_at: Optional[datetime]


@dataclass(frozen=True)
class GroupSnapshot:
    """一个组的报价输入 + 版本（缓存键组成部分）。

    FX 组同时携带正资产快照（``pair``，卖出费率）与空头快照
    （``short_pair``，买入费率 + pool_version）以及该 pair 的持久欠币
    （``short_debt``）。版本元组包含储备、两侧费率、状态、pool_version 与欠币
    时点；A/K 复用同一次捕获的市场输入和最新本人欠币。
    """

    key: GroupKey
    version: tuple
    fee_rate: Decimal
    outcomes: tuple[OutcomeSnapshot, ...] = ()      # LMSR，outcome_id 升序
    b: Optional[Decimal] = None                     # LMSR liquidity_b
    pair: Optional[FxPairSnapshot] = None           # FX 正资产
    pool_version: Optional[int] = None              # FX pair.pool_version
    short_pair: Optional[FxShortPairSnapshot] = None    # FX 空头（买入方向）
    short_debt: Optional[ShortDebtSnapshot] = None      # FX 空头持久欠币


@dataclass(frozen=True)
class DependencySet:
    """用户经济账本及一次已提交行情捕获，执行前须在 User 锁下构造。

    - ``debt`` 是**持久值**（不是含息值）；含息值在 check 内按 ``now`` 用
      ``debt_last_accrued_at`` + ``daily_rate`` 计算，保证增量 rebase 精确。
    - ``holdings`` 内层键：LMSR = ``{outcome_id: amount}``；FX = ``{pair_id: amount}``。
    - ``groups`` 只含有正持仓的组（升序）；**也包含有外币欠币的 pair**（即使
      ``User.debt==0`` 且没有正钱包）。``snapshots`` 可能额外含调用方
      ``extra_groups`` 预取的组（例如本次要买入的新 market）。
    - FX 的 ``snapshots[key].short_debt`` 是该 pair 的持久欠币；含息值在 check
      内按统一时点 T 计算。
    """

    economic_version: int
    cash: Decimal
    debt: Decimal
    debt_last_accrued_at: Optional[datetime]
    groups: tuple[GroupKey, ...]
    holdings: Mapping[GroupKey, Mapping[int, Decimal]]
    # ── WP3 扩展（WP2 报告 §2 之后落地；默认值保证位置参数兼容）──
    daily_rate: Decimal = ZERO
    lmsr_fee_rate: Decimal = ZERO
    snapshots: Mapping[GroupKey, GroupSnapshot] = field(default_factory=dict)
    short_debts: Mapping[int, ShortDebtSnapshot] = field(default_factory=dict)
    account_locked: bool = False


@dataclass(frozen=True)
class PostTradeState:
    """调用方模拟的交易后态（统一时点 T = ``check_*.now``）。

    - ``post_holdings`` 是**实际交易后**的持仓（brief §7 要求；估值用它，不用
      ``deps.holdings``）。缺省的组按"未变"处理（复用 deps 持仓与缓存）。
    - ``lmsr_q`` / ``fx_reserves`` 是交易后价格态（LMSR q 按 outcome_id 升序；
      FX 为 ``(gold_reserve, foreign_reserve)``），只给本次交易改动的品种。
    - ``short_debt``：本次订单后各 pair 的**有效外币欠币**（pair_id → 数量），
      **显式 0 表示已清仓**；未列出的 pair 从持久本金/利息/时点计息到 T。
    - ``short_reserves``：空头 touched pair 的**交易后储备** ``(gold, foreign)``，
      K 必须按它报价。``base_versions`` / ``fee_rates`` / ``statuses`` /
      ``closes_at`` 是交易后版本与费率。
    - ``base_versions`` 可选：调用方模拟所基于的品种版本（``deps.snapshots[key].version``）。
      锁外模拟的目标版本已变时返回 ``version_conflict``；执行 handler 在锁下
      构造实际目标后态，外市场后续发布不触发重试。
    """

    cash: Decimal
    debt: Decimal
    lmsr_q: Mapping[int, tuple[Decimal, ...]] = field(default_factory=dict)
    fx_reserves: Mapping[int, tuple[Decimal, Decimal]] = field(default_factory=dict)
    fee_rates: Mapping[GroupKey, Decimal] = field(default_factory=dict)
    # ── WP3 扩展（brief §7 强制）──
    post_holdings: Mapping[GroupKey, Mapping[int, Decimal]] = field(default_factory=dict)
    statuses: Mapping[GroupKey, str] = field(default_factory=dict)
    closes_at: Mapping[GroupKey, Optional[datetime]] = field(default_factory=dict)
    base_versions: Mapping[GroupKey, tuple] = field(default_factory=dict)
    # ── WP2b2 空头交易后态（统一时点 T）──
    short_debt: Mapping[int, Decimal] = field(default_factory=dict)
    short_reserves: Mapping[int, tuple[Decimal, Decimal]] = field(default_factory=dict)


@dataclass(frozen=True)
class RiskDecision:
    """增险判定结果。

    ``equity_after`` 在 K 未知/数据错误时为 ``None``（不得写 0/Infinity）；
    ``debt_after`` 始终是**含息金债**，外币欠币另见 ``short_debt_after`` 与
    ``short_cover_cost``。``risk_basis`` 仅供展示（向上量化），准入比较在
    ``check_*`` 内用未截断的 W 完成。
    """

    allowed: bool
    reason: Optional[str]
    equity_after: Optional[Decimal]
    debt_after: Decimal
    max_borrow: Optional[Decimal]
    risk_basis: Optional[Decimal] = None
    short_cover_cost: Optional[Decimal] = None
    short_debt_after: Decimal = ZERO
    # Optional full valuation projected by financed buys without a second pass.
    holdings_value: Optional[Decimal] = None


@dataclass(frozen=True)
class PostStateValuation:
    """只读的交易后完整组合估值 ``A/K/E/B``（不经过任何准入快路径）。

    与 ``check_new_risk`` 的放行判定解耦：``equity`` / ``risk_basis`` /
    ``short_cover_cost`` 在任一组件无法定价时为 ``None``，``blocked_reason``
    给出明确原因；绝不返回"看起来合理"的残缺数值（spec §5.2）。
    """

    equity: Optional[Decimal]
    risk_basis: Optional[Decimal]
    short_cover_cost: Optional[Decimal]
    holdings_value: Optional[Decimal]
    debt_after: Decimal
    blocked_reason: Optional[str]


# ────────────────────────────── 版本化缓存 ──────────────────────────────

@dataclass(frozen=True)
class RiskCacheStats:
    hits: int
    misses: int
    evictions: int
    size: int


class _GroupValueCache:
    """有界 LRU：只放"未变组在当前用户经济版本下的 L 值"（无时间敏感字段）。"""

    def __init__(self, maxsize: int = DEFAULT_CACHE_SIZE) -> None:
        self._maxsize = max(1, int(maxsize))
        self._data: "OrderedDict[tuple, Decimal]" = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def get(self, key: tuple) -> Optional[Decimal]:
        value = self._data.get(key)
        if value is None:
            self.misses += 1
            return None
        self._data.move_to_end(key)
        self.hits += 1
        return value

    def put(self, key: tuple, value: Decimal) -> None:
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)
            self.evictions += 1

    def clear(self) -> None:
        self._data.clear()

    def stats(self) -> RiskCacheStats:
        return RiskCacheStats(self.hits, self.misses, self.evictions, len(self._data))


_CACHE = _GroupValueCache()


def risk_cache_stats() -> RiskCacheStats:
    return _CACHE.stats()


def clear_risk_cache() -> None:
    """清空组值缓存（测试 fixture；版本变化本就会让旧键失效）。"""
    _CACHE.clear()


def set_risk_cache_size(maxsize: int) -> None:
    """测试钩子：缩容时立即淘汰。"""
    global _CACHE
    _CACHE = _GroupValueCache(maxsize=maxsize)


def _cache_key(
    user_id: int,
    economic_version: int,
    snapshot: GroupSnapshot,
    partial_pct: Decimal,
) -> tuple:
    return (
        int(user_id),
        int(economic_version),
        snapshot.key.product,
        int(snapshot.key.group_id),
        snapshot.version,
        partial_pct,
    )


# ────────────────────────────── 基础工具 ──────────────────────────────

class _DebtView:
    """``pending_debt`` 的结构替身（只需要 debt / debt_last_accrued_at 两个字段）。"""

    __slots__ = ("debt", "debt_last_accrued_at")

    def __init__(self, debt: Decimal, debt_last_accrued_at: Optional[datetime]) -> None:
        self.debt = debt
        self.debt_last_accrued_at = debt_last_accrued_at


def _as_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, Decimal):
        parsed = value
    else:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"{name} 不是合法 Decimal: {value!r}") from exc
    if not parsed.is_finite():
        raise ValueError(f"{name} 必须是有限 Decimal: {value!r}")
    return parsed


def _parse_decimal_or(raw: Optional[str], default: Decimal) -> Decimal:
    if raw is None or not str(raw).strip():
        return default
    try:
        parsed = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return default
    return parsed if parsed.is_finite() else default


def _q6(value: Decimal) -> Decimal:
    return _as_decimal(value, "value").quantize(Q6)


def _user_id_of(user: User) -> int:
    """不触发 lazy load 地取 user 主键（rollback 后对象可能 expired）。"""
    identity = sa_inspect(user).identity
    if identity is not None:
        return int(identity[0])
    raw = user.__dict__.get("id")
    if raw is None:
        raise ValueError("user 尚未持久化（id 为空）")
    return int(raw)


async def _refresh_user_authority(session: AsyncSession, user_id: int) -> User:
    """读**数据库权威值**并就地刷新 identity map。

    R1：``expire_on_commit=False`` 下，同 session 里先前锁行/加载的 User 实例会被
    ``select(User)`` 原样返回（不覆盖已加载属性）。经济版本、cash/debt、
    ``credit_frozen`` 必须取数据库当前行，否则外部会话抽现金/冻结后会被旧值放行。
    """
    stmt = (
        select(User)
        .where(User.id == int(user_id))
        .execution_options(populate_existing=True)
    )
    row = (await session.execute(stmt)).scalars().first()
    if row is None:
        raise ValueError(f"user not found: {user_id}")
    return row


def _effective_debt(
    debt: Decimal,
    debt_last_accrued_at: Optional[datetime],
    daily_rate: Decimal,
    now: datetime,
) -> Decimal:
    """含未落库利息的债务（与 ``accrue_interest`` 同源，不写库）。"""
    return pending_debt(_DebtView(debt, debt_last_accrued_at), daily_rate, now)


def _normalize_positions(positions: Mapping[int, Decimal]) -> dict[int, Decimal]:
    result: dict[int, Decimal] = {}
    for inner_id, amount in positions.items():
        value = _as_decimal(amount, "position amount")
        if value > ZERO:
            result[int(inner_id)] = result.get(int(inner_id), ZERO) + value
    return result


def _shift_positions(
    simulated: Mapping[int, Decimal],
    base_new: Mapping[int, Decimal],
    base_old: Mapping[int, Decimal],
) -> dict[int, Decimal]:
    """模拟持仓 + 并发增量（new − old），只保留正数。"""
    merged: dict[int, Decimal] = {}
    for inner_id in set(simulated) | set(base_new) | set(base_old):
        value = (
            _as_decimal(simulated.get(inner_id, ZERO), "simulated")
            + _as_decimal(base_new.get(inner_id, ZERO), "base_new")
            - _as_decimal(base_old.get(inner_id, ZERO), "base_old")
        )
        if value > ZERO:
            merged[int(inner_id)] = value
    return merged


def _market_changed_keys(post: PostTradeState) -> set[GroupKey]:
    keys = {GroupKey("lmsr", int(mid)) for mid in post.lmsr_q}
    keys |= {GroupKey("fx", int(pid)) for pid in post.fx_reserves}
    # 空头 touched pair：交易后储备或交易后欠币都属于需要锁内重验的模拟态。
    keys |= {GroupKey("fx", int(pid)) for pid in post.short_reserves}
    keys |= {GroupKey("fx", int(pid)) for pid in post.short_debt}
    return keys


def _group_changed(key: GroupKey, post: PostTradeState) -> bool:
    if (key in post.post_holdings or key in post.fee_rates or key in post.statuses
            or key in post.closes_at):
        return True
    if key.product == "lmsr":
        return key.group_id in post.lmsr_q
    if key.group_id in post.fx_reserves:
        return True
    return key.group_id in post.short_reserves or key.group_id in post.short_debt


def _needed_groups(deps: DependencySet, post: PostTradeState) -> tuple[GroupKey, ...]:
    keys = set(deps.groups)
    keys |= set(post.post_holdings)
    keys |= set(post.fee_rates) | set(post.statuses) | set(post.closes_at)
    keys |= set(post.base_versions)
    keys |= _market_changed_keys(post)
    return tuple(sorted(keys))


def _positions_after(
    key: GroupKey, deps: DependencySet, post: PostTradeState,
) -> dict[int, Decimal]:
    raw = post.post_holdings.get(key)
    if raw is None:
        raw = deps.holdings.get(key, {})
    return _normalize_positions(raw)


# ────────────────────────────── 发现依赖 ──────────────────────────────

def _iso(value: Optional[datetime]) -> Optional[str]:
    return None if value is None else value.isoformat()


def _fx_snapshot_version(
    pool_version: int,
    pair: FxPairSnapshot,
    short_pair: Optional[FxShortPairSnapshot],
    short_debt: Optional[ShortDebtSnapshot],
) -> tuple:
    """FX 组版本：储备 + 两侧费率 + 状态 + pool_version + 空头欠币时点。

    空头 K 对买入费率与持久欠币都敏感；把两者纳入版本后，锁内重读能发现任何
    影响 K 的并发写，绝不拿旧 K 放行（spec §11）。
    """
    return (
        int(pool_version),
        str(pair.status), bool(pair.reduce_only),
        str(pair.gold_reserve), str(pair.foreign_reserve), str(pair.sell_fee_rate),
        str(short_pair.buy_fee_rate) if short_pair is not None else None,
        str(short_debt.principal_foreign) if short_debt is not None else "0",
        str(short_debt.interest_foreign) if short_debt is not None else "0",
        _iso(short_debt.interest_last_accrued_at) if short_debt is not None else None,
    )


def _fx_group_snapshot(
    key: GroupKey,
    *,
    pair: FxPairSnapshot,
    pool_version: int,
    short_pair: Optional[FxShortPairSnapshot],
    short_debt: Optional[ShortDebtSnapshot],
) -> GroupSnapshot:
    return GroupSnapshot(
        key=key,
        version=_fx_snapshot_version(pool_version, pair, short_pair, short_debt),
        fee_rate=Decimal(pair.sell_fee_rate),
        pair=pair,
        pool_version=int(pool_version),
        short_pair=short_pair,
        short_debt=short_debt,
    )


def _captured_snapshots(keys, *, lmsr_fee_rate, short_debts):
    from app.schemas.trading_context import GroupKey as PricingKey
    from app.services.trading_prices import REGISTRY

    records = REGISTRY.capture_committed_records(
        [PricingKey(product=k.product, id=k.group_id) for k in keys])
    snapshots = {}
    for key in keys:
        record = records.get((key.product, key.group_id))
        if record is None:
            continue
        state = record.state
        if key.product == "lmsr":
            snapshots[key] = GroupSnapshot(
                key=key, version=(record.stamp, str(lmsr_fee_rate)),
                fee_rate=lmsr_fee_rate, b=Decimal(str(state.b)),
                outcomes=tuple(OutcomeSnapshot(outcome_id=oid, total_shares=q,
                    status=state.status, closes_at=state.closes_at)
                    for oid, q in zip(state.outcome_ids, state.q_dec)),
            )
        else:
            pair = FxPairSnapshot(pair_id=state.id, status=state.status,
                reduce_only=state.reduce_only, gold_reserve=state.gold_reserve,
                foreign_reserve=state.foreign_reserve, sell_fee_rate=state.sell_fee_rate)
            short_pair = FxShortPairSnapshot(pair_id=state.id, status=state.status,
                reduce_only=state.reduce_only, gold_reserve=state.gold_reserve,
                foreign_reserve=state.foreign_reserve, buy_fee_rate=state.buy_fee_rate,
                sell_fee_rate=state.sell_fee_rate, pool_version=state.pool_version)
            snapshot = _fx_group_snapshot(key, pair=pair,
                pool_version=state.pool_version, short_pair=short_pair,
                short_debt=short_debts.get(state.id))
            snapshots[key] = replace(snapshot, version=(record.stamp, *snapshot.version))
    return snapshots


async def _ensure_snapshots(session, deps, needed):
    # A missing record stays missing for this check; never recapture or SQL-fill.
    return dict(deps.snapshots)


async def discover_dependencies(
    session: AsyncSession, user_id: int, *,
    extra_groups: Sequence[GroupKey] = (), account_locked: bool = False,
) -> DependencySet:
    """Read the complete own ledger, then synchronously capture its prices once.

    Execution passes account_locked only after obtaining User FOR UPDATE. A
    discovery made before that lock is prefetch, never execution authorization.
    SQL is used for account amounts and identifiers, never market quote inputs.
    """
    uid = int(user_id)
    user = await _refresh_user_authority(session, uid)
    pos_rows = (await session.execute(select(
        Position.outcome_id, Position.amount, Outcome.market_id,
    ).join(Outcome, Outcome.id == Position.outcome_id).where(
        Position.user_id == uid, Position.amount > ZERO))).all()
    holdings = {}
    for oid, amount, mid in pos_rows:
        holdings.setdefault(GroupKey("lmsr", int(mid)), {})[int(oid)] = Decimal(amount)
    wallet_rows = (await session.execute(select(
        FxWallet.pair_id, FxWallet.foreign_amount,
    ).where(FxWallet.user_id == uid, FxWallet.foreign_amount > ZERO))).all()
    for pid, amount in wallet_rows:
        holdings[GroupKey("fx", int(pid))] = {int(pid): Decimal(amount)}
    rows = (await session.execute(select(
        FxShortPosition.pair_id, FxShortPosition.principal_foreign,
        FxShortPosition.interest_foreign, FxShortPosition.interest_last_accrued_at,
    ).where(FxShortPosition.user_id == uid,
        (FxShortPosition.principal_foreign > ZERO)
        | (FxShortPosition.interest_foreign > ZERO)))).all()
    short_debts = {int(pid): ShortDebtSnapshot(Decimal(principal), Decimal(interest), accrued)
                   for pid, principal, interest, accrued in rows}
    deps = DependencySet(economic_version=economic_version_of(user),
        cash=Decimal(user.cash), debt=Decimal(user.debt),
        debt_last_accrued_at=user.debt_last_accrued_at,
        groups=tuple(sorted(set(holdings) | {GroupKey("fx", pid) for pid in short_debts})),
        holdings=holdings, short_debts=short_debts, account_locked=account_locked)
    # These are existing maintenance-frozen global rules, separate from markets.
    deps = await _current_rates(session, deps)
    keys = tuple(sorted(set(deps.groups) | set(extra_groups)))
    return replace(deps, snapshots=_captured_snapshots(keys,
        lmsr_fee_rate=deps.lmsr_fee_rate, short_debts=short_debts))


# ────────────────────────────── 组估值 ──────────────────────────────

def _quote_group_value(
    snapshot: GroupSnapshot,
    positions: Mapping[int, Decimal],
    post: PostTradeState,
) -> Decimal:
    """单组整组清算价值（纯函数）；不可执行 / 数据异常 → 0（保守）。"""
    key = snapshot.key
    held = _normalize_positions(positions)
    if key.product == "lmsr":
        if snapshot.b is None:
            return ZERO
        outcomes = list(snapshot.outcomes)
        q_override = post.lmsr_q.get(key.group_id)
        if q_override is not None:
            if len(q_override) != len(outcomes):
                logger.warning(
                    "risk: lmsr market %s 的 post q 长度 %d 与 outcomes %d 不一致，按 L=0",
                    key.group_id, len(q_override), len(outcomes),
                )
                return ZERO
            outcomes = [
                replace(item, total_shares=_as_decimal(q, "post q"))
                for item, q in zip(outcomes, q_override)
            ]
        if key in post.statuses:
            outcomes = [replace(item, status=post.statuses[key]) for item in outcomes]
        if key in post.closes_at:
            outcomes = [replace(item, closes_at=post.closes_at[key]) for item in outcomes]
        fee = _as_decimal(post.fee_rates.get(key, snapshot.fee_rate), "fee_rate")
        if not outcomes:
            return ZERO
        quote = quote_lmsr_group(
            outcomes, held,
            market_id=key.group_id, b=snapshot.b, fee_rate=fee,
            mode="full", partial_pct=ONE,
        )
        return ZERO if quote.blocked_reason is not None else quote.net

    pair = snapshot.pair
    if pair is None:
        return ZERO
    reserves = post.fx_reserves.get(key.group_id)
    if reserves is None:
        # 同一 pair 的空头成交也会改变储备；正资产腿用同一交易后储备估值。
        reserves = post.short_reserves.get(key.group_id)
    if reserves is not None:
        pair = replace(
            pair,
            gold_reserve=_as_decimal(reserves[0], "post gold_reserve"),
            foreign_reserve=_as_decimal(reserves[1], "post foreign_reserve"),
        )
    if key in post.statuses:
        pair = replace(pair, status=post.statuses[key])
    if key in post.fee_rates:
        pair = replace(pair, sell_fee_rate=_as_decimal(post.fee_rates[key], "fee_rate"))
    amount = sum(held.values(), ZERO)
    quote = quote_fx_group(pair, foreign_amount=amount, mode="full", partial_pct=ONE)
    return ZERO if quote.blocked_reason is not None else quote.gold_out


async def _collateral_value(
    session: AsyncSession,
    *,
    user_id: int,
    deps: DependencySet,
    post: PostTradeState,
    partial_pct: Decimal,
    cache_only: bool = False,
) -> tuple[Decimal, bool]:
    """全组合清算价值 Σ L_group（LMSR + FX 一起算）。

    返回 ``(value, complete)``：``value`` 是已完成部分的清算价值。``cache_only=True``
    时**不做任何报价**（无债快路径），只命中缓存，``complete=False`` 表示有组缺缓存
    或缺快照。``cache_only=False`` 时只用本次检查固定捕获的已提交快照报价，不重新
    捕获或从 SQL 补齐；缺少所需快照即 ``complete=False``。此时 ``value`` 是缺该组
    的下界，调用方若需要完整净值必须把整本书标为未知，不得把下界当完整估值
    （spec §5.2）。
    """
    needed = _needed_groups(deps, post)
    snapshots = dict(deps.snapshots) if cache_only else await _ensure_snapshots(session, deps, needed)
    total = ZERO
    complete = True
    for key in needed:
        if not _positions_after(key, deps, post):
            continue
        snapshot = snapshots.get(key)
        if snapshot is None:
            complete = False
            continue  # 无法估值 → 0（保守）
        changed = _group_changed(key, post)
        if not changed:
            cache_key = _cache_key(user_id, deps.economic_version, snapshot, partial_pct)
            cached = _CACHE.get(cache_key)
            if cached is not None:
                total += cached
                continue
            if cache_only:
                # 无债快路径：不做任何报价，缺缓存即视为不完整（保守）。
                complete = False
                continue
        value = _quote_group_value(snapshot, _positions_after(key, deps, post), post)
        total += value
        if not changed:
            _CACHE.put(
                _cache_key(user_id, deps.economic_version, snapshot, partial_pct), value,
            )
    return total, complete


# ────────────────────────────── 版本复检 / rebase ──────────────────────────────

def _post_state_stale(
    old: DependencySet, new: DependencySet, post: PostTradeState,
) -> bool:
    """调用方模拟所基于的价格/状态版本是否已被并发写推进。"""
    simulated = _market_changed_keys(post) | set(post.statuses) | set(post.closes_at)
    for key, version in post.base_versions.items():
        if key not in simulated:
            continue
        snapshot = new.snapshots.get(key)
        if snapshot is not None and version is not None and snapshot.version != version:
            return True
    for key in _market_changed_keys(post):
        old_snapshot = old.snapshots.get(key)
        new_snapshot = new.snapshots.get(key)
        if (old_snapshot is not None and new_snapshot is not None
                and old_snapshot.version != new_snapshot.version):
            return True
    return False


def _rebase_post(
    old: DependencySet, new: DependencySet, post: PostTradeState,
) -> PostTradeState:
    """把并发增量（new − old）重放到调用方的模拟后态上。

    调用方的交易是相对 old 的一个 delta；并发写改的是 base，所以同样的 delta
    叠加到 new 上即可（cash/debt 直接加差，holdings 逐腿加差）。

    R2：``post_holdings[key] = {}``（显式清仓）必须保留空映射——丢了它会退回
    ``deps.holdings`` 旧持仓，把已清仓的组按旧值计价而错误放行。
    """
    holdings: dict[GroupKey, dict[int, Decimal]] = {}
    for key in set(old.holdings) | set(new.holdings) | set(post.post_holdings):
        base_old = old.holdings.get(key, {})
        base_new = new.holdings.get(key, {})
        simulated = post.post_holdings.get(key, base_old)
        merged = _shift_positions(simulated, base_new, base_old)
        if merged or key in post.post_holdings:
            holdings[key] = merged
    return replace(
        post,
        cash=post.cash + (new.cash - old.cash),
        debt=post.debt + (new.debt - old.debt),
        post_holdings=holdings,
    )


async def _current_rates(session: AsyncSession, deps: DependencySet) -> DependencySet:
    # Column reads bypass both the TTL cache and ORM identity-map snapshots.
    rows = (await session.execute(select(SiteConfig.key, SiteConfig.value).where(
        SiteConfig.key.in_([LOAN_DAILY_RATE_KEY, SELL_FEE_RATE_KEY, credit_flags.KEY_CREDIT_NEW_RISK_FROZEN])
    ))).all()
    raw = dict(rows)
    credit_flags.set_new_risk_frozen(
        str(raw.get(credit_flags.KEY_CREDIT_NEW_RISK_FROZEN, "false")).strip().lower()
        in {"true", "1", "yes", "on"}
    )
    return replace(deps,
        daily_rate=_parse_decimal_or(raw.get(LOAN_DAILY_RATE_KEY), ZERO),
        lmsr_fee_rate=_parse_decimal_or(raw.get(SELL_FEE_RATE_KEY), ZERO))


async def _prepare_post(session, *, user_id, deps, post):
    if deps.account_locked:
        return deps, post
    fresh = await discover_dependencies(session, user_id,
        extra_groups=_needed_groups(deps, post), account_locked=True)
    if _post_state_stale(deps, fresh, post):
        return None
    if (fresh.economic_version != deps.economic_version or fresh.cash != deps.cash
            or fresh.debt != deps.debt or fresh.holdings != deps.holdings
            or fresh.short_debts != deps.short_debts):
        # An absolute short simulation cannot be rebased from an old obligation.
        if post.short_debt:
            return None
        post = _rebase_post(deps, fresh, post)
    else:
        # Honor callers which have already normalized effective gold debt at T.
        fresh = replace(fresh, debt_last_accrued_at=deps.debt_last_accrued_at)
    return fresh, post


# ────────────────────────────── 对外检查 ──────────────────────────────

def _freeze_reason(user: User, hot_frozen: bool) -> Optional[str]:
    if bool(getattr(user, "credit_frozen", False)):
        return REASON_CREDIT_FROZEN
    if hot_frozen:
        return REASON_FROZEN_BY_OPERATOR
    return None


def _deny(
    reason: str, *, cash: Decimal, debt_after: Decimal,
) -> RiskDecision:
    return RiskDecision(
        allowed=False,
        reason=reason,
        equity_after=_q6(cash),
        debt_after=_q6(debt_after),
        max_borrow=None,
    )


def _has_short_obligation(deps: DependencySet, post: PostTradeState) -> bool:
    """是否存在需要完整风控的外币欠币（显式 0 视为已清仓）。

    ``User.debt==0`` 不是快路径理由：只要有未清偿外币本金/利息，或本次订单
    产生了欠币，就必须走完整依赖与风险检查（spec §6.3/§11）。
    """
    for value in post.short_debt.values():
        try:
            if _as_decimal(value, "short_debt") > ZERO:
                return True
        except ValueError:
            # 非法模拟欠币：视为有义务，让后续 _short_risk_after 明确 blocked。
            return True
    debts = dict(deps.short_debts)
    debts.update({k.group_id: v.short_debt for k, v in deps.snapshots.items()
                  if k.product == "fx" and v.short_debt is not None})
    for pid, debt in debts.items():
        if post.short_debt.get(pid) == ZERO:
            continue
        if debt.principal_foreign > ZERO or debt.interest_foreign > ZERO:
            return True
    return False


def _short_pair_ids(deps: DependencySet, post: PostTradeState) -> tuple[int, ...]:
    ids = set(deps.short_debts) | {int(pid) for pid in post.short_debt}
    ids |= {int(pid) for pid in post.short_reserves}
    for key in deps.groups:
        if key.product != "fx":
            continue
        snapshot = deps.snapshots.get(key)
        if snapshot is not None and snapshot.short_debt is not None:
            ids.add(key.group_id)
    return tuple(sorted(ids))


def _short_risk_after(
    deps: DependencySet, post: PostTradeState, now: datetime,
) -> tuple[Optional[Decimal], Decimal, Optional[str], Optional[str]]:
    """交易后各 pair 的整仓回补成本与总外币欠币（统一时点 T = ``now``）。

    返回 ``(K, foreign_debt_total, unknown_reason, not_executable_reason)``：

    - 任一 pair 无法完整报价 → ``K=None`` 且给出明确原因；
    - 数学报价有限但市场全停 → ``K`` 有值、``not_executable_reason`` 明确；
    - 未在 ``post.short_debt`` 列出的 pair 从持久本金/利息/时点计息到 ``now``；
    - 每个 touched pair 的 K 是一次 ``quote_buy_exact_out``（交易后储备 + 当前
      买入费率 + 该 pair 全部含息欠币），不同 pair 的 K 相加（spec §5/§6）。
    """
    cover = ZERO
    total = ZERO
    unknown: Optional[str] = None
    not_executable: Optional[str] = None
    for pid in _short_pair_ids(deps, post):
        key = GroupKey("fx", pid)
        snapshot = deps.snapshots.get(key)
        if snapshot is None:
            unknown = unknown or REASON_SHORT_SNAPSHOT_MISSING
            continue
        if pid in post.short_debt:
            try:
                debt = _as_decimal(post.short_debt[pid], "short_debt")
            except ValueError:
                unknown = unknown or REASON_INVALID_SHORT_DEBT
                continue
            if debt < ZERO:
                unknown = unknown or REASON_INVALID_SHORT_DEBT
                continue
        else:
            if snapshot.short_debt is None:
                continue
            try:
                debt = pending_short_debt(snapshot.short_debt, deps.daily_rate, now)
            except (ValueError, ArithmeticError):
                unknown = unknown or REASON_INVALID_SHORT_DEBT
                continue
        if debt <= ZERO:
            continue
        total += debt
        pair = snapshot.short_pair
        if pair is None:
            unknown = unknown or REASON_SHORT_SNAPSHOT_MISSING
            continue
        reserves = post.short_reserves.get(pid)
        if reserves is not None:
            pair = replace(
                pair,
                gold_reserve=_as_decimal(reserves[0], "post gold_reserve"),
                foreign_reserve=_as_decimal(reserves[1], "post foreign_reserve"),
            )
        quote = quote_fx_short_group(pair, foreign_debt=debt)
        if quote.gold_in is None:
            unknown = unknown or (quote.blocked_reason or BLOCKED_SHORT_QUOTE_FAILED)
        else:
            cover += quote.gold_in
        if not quote.executable and quote.blocked_reason is not None:
            not_executable = not_executable or quote.blocked_reason
    if unknown is not None:
        return None, total, unknown, not_executable
    return cover, total, None, not_executable


def _decide_short(
    *,
    thresholds: RiskThresholds,
    cash_after: Decimal,
    holdings_value: Decimal,
    debt_after: Decimal,
    cover: Decimal,
    short_total: Decimal,
    not_executable: Optional[str],
) -> RiskDecision:
    """空头/混合组合的交易后判定：准入用未截断 W，展示 B 单独向上量化。"""
    equity_raw = cash_after + holdings_value - debt_after - cover
    equity = _q6(equity_raw)
    basis = thresholds.risk_basis(
        debt=debt_after, positive_assets=holdings_value, short_cover=cover,
    ).quantize(Q6, rounding=ROUND_CEILING)
    if not_executable is not None:
        # 数学有限但全停：K/E 有值，增险仍因不可执行被拒（spec §5.2/§8.2）。
        return RiskDecision(
            False, not_executable, equity, debt_after, ZERO,
            risk_basis=basis, short_cover_cost=cover, short_debt_after=short_total,
        )
    max_borrow = thresholds.max_new_gold_loan(
        equity=equity_raw, debt=debt_after,
        positive_assets=holdings_value, short_cover=cover,
    )
    if thresholds.admits(
        equity=equity_raw, debt=debt_after,
        positive_assets=holdings_value, short_cover=cover,
    ):
        return RiskDecision(
            True, None, equity, debt_after, max_borrow,
            risk_basis=basis, short_cover_cost=cover, short_debt_after=short_total,
        )
    return RiskDecision(
        False, REASON_INSUFFICIENT_INITIAL_MARGIN, equity, debt_after, max_borrow,
        risk_basis=basis, short_cover_cost=cover, short_debt_after=short_total,
    )


async def check_new_risk(
    session: AsyncSession,
    *,
    user: User,
    deps: DependencySet,
    post: PostTradeState,
    thresholds: RiskThresholds,
    partial_pct: Decimal,
    now: datetime,
    include_valuation: bool = False,
) -> RiskDecision:
    """交易后风险检查（借款 / 买入 / 开仓等增险路径）。

    - 冻结（用户级 / 运营闸）→ 拒绝增险；
    - 锁外预取须重读本人账本，陈旧目标模拟返回 `version_conflict`；
    - `D_after == 0` **且无外币欠币** → 直接放行（不报价，旧快路径）；
    - 否则按**全组合**整组清算价值 + 各空头 pair 的整仓回补成本检查。

    数值决策用 ``L(L−1)E >= W``（未量化的 E）。空头 K 未知时 equity/risk_basis/
    short_cover_cost 为 ``None``（绝不写 0/Infinity），理论借款额度按 0。
    """
    pct = _as_decimal(partial_pct, "partial_pct")
    uid = _user_id_of(user)
    prepared = await _prepare_post(session, user_id=uid, deps=deps, post=post)
    if prepared is None:
        return _deny(REASON_VERSION_CONFLICT, cash=post.cash, debt_after=post.debt)
    deps, post = prepared
    authority = user
    if economic_version_of(authority) != deps.economic_version:
        return _deny(REASON_VERSION_CONFLICT, cash=post.cash, debt_after=post.debt)
    debt_after = _effective_debt(post.debt, deps.debt_last_accrued_at, deps.daily_rate, now)
    reason = _freeze_reason(authority, credit_flags.new_risk_frozen())
    if reason is not None:
        return _deny(reason, cash=post.cash, debt_after=debt_after)

    has_short = _has_short_obligation(deps, post)
    if debt_after <= ZERO and not has_short:
        # 无债快路径：不存在初始保证金约束；不报价，equity/max_borrow 只用缓存值，
        # 缓存不全时 max_borrow=None（需要精确 E 的调用方走 WP2 valuation）。
        holdings_value, complete = await _collateral_value(
            session, user_id=uid, deps=deps, post=post, partial_pct=pct, cache_only=True,
        )
        equity = _q6(post.cash + holdings_value)
        max_borrow = thresholds.max_borrow(equity, ZERO) if complete else None
        return RiskDecision(True, None, equity, ZERO, max_borrow)

    holdings_value, complete = await _collateral_value(
        session, user_id=uid, deps=deps, post=post, partial_pct=pct,
    )
    if not complete:
        return RiskDecision(False, REASON_INCOMPLETE_ASSET_VALUATION, None, debt_after, ZERO)

    if not has_short:
        # 无空头账户逐字保持旧决策行为（K=0 时 W 形式与旧式代数等价）。
        equity = _q6(post.cash + holdings_value - debt_after)
        max_borrow = thresholds.max_borrow(equity, debt_after)
        projection = (dict(holdings_value=holdings_value, short_cover_cost=ZERO,
                           risk_basis=thresholds.risk_basis(debt=debt_after,
                               positive_assets=holdings_value, short_cover=ZERO)
                               .quantize(Q6, rounding=ROUND_CEILING))
                      if include_valuation else {})
        if equity >= thresholds.r_initial * debt_after:
            return RiskDecision(True, None, equity, debt_after, max_borrow, **projection)
        return RiskDecision(
            False, REASON_INSUFFICIENT_INITIAL_MARGIN, equity, debt_after, max_borrow,
            **projection,
        )

    cover, short_total, unknown, not_exec = _short_risk_after(deps, post, now)
    if unknown is not None:
        return RiskDecision(
            # K 未知：E/B/K 均为 None；理论借款额度按 0（绝不放行增险）。
            False, unknown, None, debt_after, ZERO,
            risk_basis=None, short_cover_cost=None, short_debt_after=short_total,
        )
    decision = _decide_short(
        thresholds=thresholds, cash_after=post.cash,
        holdings_value=holdings_value, debt_after=debt_after,
        cover=cover, short_total=short_total, not_executable=not_exec,
    )
    return replace(decision, holdings_value=holdings_value) if include_valuation else decision


async def value_post_state(
    session: AsyncSession,
    *,
    user_id: int,
    deps: DependencySet,
    post: PostTradeState,
    thresholds: RiskThresholds,
    now: datetime,
) -> PostStateValuation:
    """完整交易后 ``A/K/E/B``（只读、advisory）：缓存缺失也现算。

    与 ``check_new_risk`` 共用 ``_collateral_value`` / ``_short_risk_after`` /
    ``thresholds.risk_basis``，因此不是第二套风险公式；区别在于它**不套用**
    无债/无空头准入快路径，也不返回放行判定，只回答"这笔交易后组合值多少"。

    - 正资产任一组补不到快照 → ``equity``/``risk_basis``/``short_cover_cost``
      为 ``None``，原因是 :data:`REASON_INCOMPLETE_ASSET_VALUATION`；
    - 任一空头无法完整报价 → 同上并把 ``_short_risk_after`` 的原因透出；
    - 已知但不可执行的资产按 ``L=0`` 计（与准入估值一致），不算未知。
    """
    debt_after = _effective_debt(
        post.debt, deps.debt_last_accrued_at, deps.daily_rate, now,
    )
    holdings_value, complete = await _collateral_value(
        session, user_id=int(user_id), deps=deps, post=post, partial_pct=ONE,
    )
    if not complete:
        return PostStateValuation(
            equity=None,
            risk_basis=None,
            short_cover_cost=None,
            holdings_value=None,
            debt_after=debt_after,
            blocked_reason=REASON_INCOMPLETE_ASSET_VALUATION,
        )
    cover, _short_total, unknown, _not_executable = _short_risk_after(
        deps, post, now,
    )
    if unknown is not None:
        return PostStateValuation(
            equity=None,
            risk_basis=None,
            short_cover_cost=None,
            holdings_value=holdings_value,
            debt_after=debt_after,
            blocked_reason=unknown,
        )
    equity = _q6(post.cash + holdings_value - debt_after - cover)
    risk_basis = thresholds.risk_basis(
        debt=debt_after, positive_assets=holdings_value, short_cover=cover,
    ).quantize(Q6, rounding=ROUND_CEILING)
    return PostStateValuation(
        equity=equity,
        risk_basis=risk_basis,
        short_cover_cost=cover,
        holdings_value=holdings_value,
        debt_after=debt_after,
        blocked_reason=None,
    )


async def check_cash_spend(
    session: AsyncSession,
    *,
    user: User,
    deps: DependencySet,
    spend: Decimal,
    thresholds: RiskThresholds,
    partial_pct: Decimal,
    now: datetime,
) -> RiskDecision:
    """消费 / 转出现金的风险检查（现金减少 = 增险）。

    持仓、市场态与外币欠币不变（用版本化缓存 + 锁内重读），只把现金减去
    ``spend`` 后重算 E / W。无金债但有欠币的账户同样必须完整判定。
    """
    amount = _as_decimal(spend, "spend")
    if amount < ZERO:
        raise ValueError(f"spend 不能为负: {amount!r}")
    pct = _as_decimal(partial_pct, "partial_pct")
    uid = _user_id_of(user)
    if not deps.account_locked:
        deps = await discover_dependencies(session, uid, account_locked=True)
    authority = user
    debt_after = _effective_debt(deps.debt, deps.debt_last_accrued_at, deps.daily_rate, now)
    cash_after = deps.cash - amount
    reason = _freeze_reason(authority, credit_flags.new_risk_frozen())
    if reason is not None:
        return _deny(reason, cash=cash_after, debt_after=debt_after)
    post = PostTradeState(cash=cash_after, debt=deps.debt)

    has_short = _has_short_obligation(deps, post)
    if debt_after <= ZERO and not has_short:
        holdings_value, complete = await _collateral_value(
            session, user_id=uid, deps=deps, post=post, partial_pct=pct, cache_only=True,
        )
        equity = _q6(cash_after + holdings_value)
        max_borrow = thresholds.max_borrow(equity, ZERO) if complete else None
        return RiskDecision(True, None, equity, ZERO, max_borrow)

    holdings_value, complete = await _collateral_value(
        session, user_id=uid, deps=deps, post=post, partial_pct=pct,
    )
    if not complete:
        return RiskDecision(False, REASON_INCOMPLETE_ASSET_VALUATION, None, debt_after, ZERO)

    if not has_short:
        equity = _q6(cash_after + holdings_value - debt_after)
        max_borrow = thresholds.max_borrow(equity, debt_after)
        if equity >= thresholds.r_initial * debt_after:
            return RiskDecision(True, None, equity, debt_after, max_borrow)
        return RiskDecision(
            False, REASON_INSUFFICIENT_INITIAL_MARGIN, equity, debt_after, max_borrow,
        )

    cover, short_total, unknown, not_exec = _short_risk_after(deps, post, now)
    if unknown is not None:
        return RiskDecision(
            # K 未知：E/B/K 均为 None；理论借款额度按 0（绝不放行增险）。
            False, unknown, None, debt_after, ZERO,
            risk_basis=None, short_cover_cost=None, short_debt_after=short_total,
        )
    return _decide_short(
        thresholds=thresholds, cash_after=cash_after,
        holdings_value=holdings_value, debt_after=debt_after,
        cover=cover, short_total=short_total, not_executable=not_exec,
    )
