"""统一风险检查（计划 §3.2 冻结签名；spec §4 / §6.2）。

调用约定：

- 调用方**已持**相关品种门闩（``credit/gates.py``），并在 DB 事务内、用户行锁之后调用。
- ``deps`` 是锁外 ``discover_dependencies`` 的快照；``check_*`` 内用
  ``user.economic_version`` 复检，不一致时有界重试重新发现依赖，并把调用方给的
  交易后态按**增量** rebase（``cash/debt/post_holdings`` 重放同样的 delta），
  超限返回 ``version_conflict`` 拒绝——绝不用旧快照放行（spec §6.2 第 4 条）。
- 本模块不写库、不 commit、不发 writer 命令、不持全局锁、不做定时调度；
  强平只由 WP7 的定时扫描触发。
- **权威读取（复审 R1）**：``discover_dependencies`` 与 ``check_*`` 用
  ``populate_existing`` 刷新 User 行，版本 / cash / debt / ``credit_frozen`` 一律取
  数据库当前值——同 session 里先前锁行或加载的旧实例不会让外部并发改动被忽略。
  代价是该实例上的**未提交内存修改会被丢弃**，所以调用方必须在应用变更**之前**
  调用 ``check_*``（计划 WP6 的顺序本来就是先检查后落库）。
- **价格/状态版本（复审 R3）**：锁内重读本次模拟涉及的品种快照；调用方声明的
  ``base_versions`` 或 deps 快照版本被推进即 ``version_conflict`` 拒绝，**不要求
  user 版本也变化**。纯抵押品种只刷新快照重估，不拒绝。

判定（spec §4）：

    D_after == 0                  → 直接放行（无债快路径，不做保证金比较，不报价）
    D_after > 0                   → E_after >= R_initial × D_after 才放行
    E_after = cash_after + Σ L_group(post_holdings) − D_after_effective

``L_group`` 是两个产品共用的**整组清算价值**（LMSR 滚动 q + FX AMM，含滑点与手续费，
不可执行组 L=0）；不是瞬时价 × 数量，也不是 MTM。``D_after`` 用
``loan_service.pending_debt``（与计息同源，含未落库利息）。

``credit_frozen``（用户级）与 ``credit_new_risk_frozen``（运营热闸）都拒绝**增险**：
这两个函数只被"增险"路径调用；还款 / 减仓 / 强平不经过这里。
版本化缓存只缓存**未变组的 L 值**，键含用户经济版本与品种价格/状态/费率版本，
不缓存 effective debt / equity，也不依赖 TTL 猜新鲜度（spec §6.3）。
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Mapping, Optional, Sequence

from sqlalchemy import inspect as sa_inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import Market, Outcome, Position, SiteConfig, User
from app.models.fx import FxPair, FxWallet
from app.services import site_config
from app.services.credit import flags as credit_flags
from app.services.credit.fx_quote import FxPairSnapshot, quote_fx_group
from app.services.credit.keys import GroupKey
from app.services.credit.lmsr_quote import OutcomeSnapshot, quote_lmsr_group
from app.services.credit.thresholds import RiskThresholds
from app.services.credit.version import economic_version_of
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


class _StalePostState(Exception):
    """调用方模拟的交易后价格/状态基于已过期的品种版本（R3）：安全拒绝。"""


# ────────────────────────────── 数据结构 ──────────────────────────────

@dataclass(frozen=True)
class GroupSnapshot:
    """一个组的报价输入 + 版本（缓存键组成部分）。"""

    key: GroupKey
    version: tuple
    fee_rate: Decimal
    outcomes: tuple[OutcomeSnapshot, ...] = ()      # LMSR，outcome_id 升序
    b: Optional[Decimal] = None                     # LMSR liquidity_b
    pair: Optional[FxPairSnapshot] = None           # FX
    pool_version: Optional[int] = None              # FX pair.pool_version


@dataclass(frozen=True)
class DependencySet:
    """锁外发现的用户经济状态 + 全组合品种快照。

    - ``debt`` 是**持久值**（不是含息值）；含息值在 check 内按 ``now`` 用
      ``debt_last_accrued_at`` + ``daily_rate`` 计算，保证增量 rebase 精确。
    - ``holdings`` 内层键：LMSR = ``{outcome_id: amount}``；FX = ``{pair_id: amount}``。
    - ``groups`` 只含有正持仓的组（升序）；``snapshots`` 可能额外含调用方
      ``extra_groups`` 预取的组（例如本次要买入的新 market）。
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


@dataclass(frozen=True)
class PostTradeState:
    """调用方模拟的交易后态。

    - ``post_holdings`` 是**实际交易后**的持仓（brief §7 要求；估值用它，不用
      ``deps.holdings``）。缺省的组按"未变"处理（复用 deps 持仓与缓存）。
    - ``lmsr_q`` / ``fx_reserves`` 是交易后价格态（LMSR q 按 outcome_id 升序；
      FX 为 ``(gold_reserve, foreign_reserve)``），只给本次交易改动的品种。
    - ``fee_rates`` / ``statuses`` / ``closes_at`` 是交易后费率与状态版本。
    - ``base_versions`` 可选：调用方模拟所基于的品种版本（``deps.snapshots[key].version``）。
      提供时，若锁内重发现发现版本已变 → 直接 ``version_conflict``，不拿旧价格态放行。
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


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: Optional[str]
    equity_after: Decimal
    debt_after: Decimal
    max_borrow: Optional[Decimal]


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
    return keys


def _group_changed(key: GroupKey, post: PostTradeState) -> bool:
    if (key in post.post_holdings or key in post.fee_rates or key in post.statuses
            or key in post.closes_at):
        return True
    if key.product == "lmsr":
        return key.group_id in post.lmsr_q
    return key.group_id in post.fx_reserves


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

def _writer_state(market_id: int):
    from app.services.market_writer import WRITER

    return WRITER.get_state(market_id)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return None if value is None else value.isoformat()


async def _load_snapshots(
    session: AsyncSession,
    keys: Sequence[GroupKey],
    *,
    lmsr_fee_rate: Decimal,
    lmsr_meta: Optional[Mapping[int, tuple[str, Optional[datetime], Decimal]]] = None,
    fx_meta: Optional[Mapping[int, FxPairSnapshot]] = None,
    fx_pool_versions: Optional[Mapping[int, int]] = None,
) -> dict[GroupKey, GroupSnapshot]:
    """批量加载组快照（LMSR：market + 全 outcomes；FX：pair）。

    已由 position/wallet 查询带出的 meta 直接复用，避免重复 SELECT。
    找不到的品种不产出快照（调用方按不可估值 → L=0 保守处理）。
    """
    lmsr_ids = sorted({k.group_id for k in keys if k.product == "lmsr"})
    fx_ids = sorted({k.group_id for k in keys if k.product == "fx"})
    markets: dict[int, tuple[str, Optional[datetime], Decimal]] = dict(lmsr_meta or {})
    pairs: dict[int, FxPairSnapshot] = dict(fx_meta or {})
    pool_versions: dict[int, int] = dict(fx_pool_versions or {})

    missing_markets = [mid for mid in lmsr_ids if mid not in markets]
    if missing_markets:
        rows = (await session.execute(
            select(Market.id, Market.status, Market.closes_at, Market.liquidity_b)
            .where(Market.id.in_(missing_markets))
        )).all()
        for market_id, status, closes_at, liquidity_b in rows:
            markets[int(market_id)] = (status, closes_at, Decimal(liquidity_b))

    outcomes_by_market: dict[int, list[tuple[int, Decimal]]] = {}
    if lmsr_ids:
        rows = (await session.execute(
            select(Outcome.id, Outcome.market_id, Outcome.total_shares)
            .where(Outcome.market_id.in_(lmsr_ids))
            .order_by(Outcome.market_id, Outcome.id)
        )).all()
        for outcome_id, market_id, total_shares in rows:
            outcomes_by_market.setdefault(int(market_id), []).append(
                (int(outcome_id), Decimal(total_shares)),
            )

    missing_pairs = [pid for pid in fx_ids if pid not in pairs]
    if missing_pairs:
        rows = (await session.execute(
            select(
                FxPair.id, FxPair.status, FxPair.reduce_only, FxPair.gold_reserve,
                FxPair.foreign_reserve, FxPair.sell_fee_rate, FxPair.pool_version,
            ).where(FxPair.id.in_(missing_pairs))
        )).all()
        for (pair_id, status, reduce_only, gold_reserve, foreign_reserve,
             sell_fee_rate, pool_version) in rows:
            pid = int(pair_id)
            pairs[pid] = FxPairSnapshot(
                pair_id=pid,
                status=status,
                reduce_only=bool(reduce_only),
                gold_reserve=Decimal(gold_reserve),
                foreign_reserve=Decimal(foreign_reserve),
                sell_fee_rate=Decimal(sell_fee_rate),
            )
            pool_versions[pid] = int(pool_version)

    result: dict[GroupKey, GroupSnapshot] = {}
    for key in sorted(set(keys)):
        if key.product == "lmsr":
            meta = markets.get(key.group_id)
            if meta is None:
                logger.warning("risk: market %s 不存在，组快照缺失（按 L=0 保守处理）", key.group_id)
                continue
            status, closes_at, liquidity_b = meta
            outcomes = tuple(
                OutcomeSnapshot(
                    outcome_id=outcome_id, total_shares=shares,
                    status=status, closes_at=closes_at,
                )
                for outcome_id, shares in outcomes_by_market.get(key.group_id, [])
            )
            q = tuple(item.total_shares for item in outcomes)
            mirror = None
            state = _writer_state(key.group_id)
            if state is not None:
                # 镜像 q（6dp）纳入版本：commit 后镜像推进也要让缓存失效
                mirror = (
                    tuple(Decimal(item) for item in state.q_dec),
                    str(state.status),
                    _iso(state.closes_at),
                )
            version = (
                q, str(liquidity_b), str(status), _iso(closes_at),
                str(lmsr_fee_rate), mirror,
            )
            result[key] = GroupSnapshot(
                key=key, version=version, fee_rate=lmsr_fee_rate,
                outcomes=outcomes, b=Decimal(liquidity_b),
            )
        else:
            pair = pairs.get(key.group_id)
            if pair is None:
                logger.warning("risk: fx pair %s 不存在，组快照缺失（按 L=0 保守处理）", key.group_id)
                continue
            version = (
                int(pool_versions.get(key.group_id, 0)),
                str(pair.status), bool(pair.reduce_only),
                str(pair.gold_reserve), str(pair.foreign_reserve), str(pair.sell_fee_rate),
            )
            result[key] = GroupSnapshot(
                key=key, version=version, fee_rate=Decimal(pair.sell_fee_rate),
                pair=pair, pool_version=int(pool_versions.get(key.group_id, 0)),
            )
    return result


async def _ensure_snapshots(
    session: AsyncSession,
    deps: DependencySet,
    needed: Sequence[GroupKey],
) -> dict[GroupKey, GroupSnapshot]:
    """补齐 deps 里没有的组快照（例如本次买入的新 market）。"""
    missing = [key for key in needed if key not in deps.snapshots]
    merged = dict(deps.snapshots)
    if missing:
        merged.update(await _load_snapshots(
            session, missing, lmsr_fee_rate=deps.lmsr_fee_rate,
        ))
    return merged


async def discover_dependencies(
    session: AsyncSession,
    user_id: int,
    *,
    extra_groups: Sequence[GroupKey] = (),
) -> DependencySet:
    """锁外批量读取用户经济状态 + 全组合品种快照（固定 4 条 SELECT + site_config）。

    ``extra_groups`` 可预取本次交易涉及、但用户当前无持仓的品种快照（可选优化；
    ``check_*`` 在缺快照时也会自行补齐）。
    """
    uid = int(user_id)
    # R1：必须 populate_existing，否则返回同 session identity map 里的旧 User，
    # 外部并发改动（抽现金/冻结/bump 版本）会被忽略。
    user = await _refresh_user_authority(session, uid)

    pos_rows = (await session.execute(
        select(
            Position.outcome_id, Position.amount, Outcome.market_id,
            Market.status, Market.closes_at, Market.liquidity_b,
        )
        .join(Outcome, Outcome.id == Position.outcome_id)
        .join(Market, Market.id == Outcome.market_id)
        .where(Position.user_id == uid, Position.amount > ZERO)
    )).all()

    holdings: dict[GroupKey, dict[int, Decimal]] = {}
    lmsr_meta: dict[int, tuple[str, Optional[datetime], Decimal]] = {}
    for outcome_id, amount, market_id, status, closes_at, liquidity_b in pos_rows:
        key = GroupKey("lmsr", int(market_id))
        holdings.setdefault(key, {})[int(outcome_id)] = Decimal(amount)
        lmsr_meta[int(market_id)] = (status, closes_at, Decimal(liquidity_b))

    wallet_rows = (await session.execute(
        select(
            FxWallet.pair_id, FxWallet.foreign_amount, FxPair.status, FxPair.reduce_only,
            FxPair.gold_reserve, FxPair.foreign_reserve, FxPair.sell_fee_rate,
            FxPair.pool_version,
        )
        .join(FxPair, FxPair.id == FxWallet.pair_id)
        .where(FxWallet.user_id == uid, FxWallet.foreign_amount > ZERO)
    )).all()
    fx_meta: dict[int, FxPairSnapshot] = {}
    fx_pool_versions: dict[int, int] = {}
    for (pair_id, amount, status, reduce_only, gold_reserve, foreign_reserve,
         sell_fee_rate, pool_version) in wallet_rows:
        pid = int(pair_id)
        key = GroupKey("fx", pid)
        holdings.setdefault(key, {})[pid] = Decimal(amount)
        fx_meta[pid] = FxPairSnapshot(
            pair_id=pid, status=status, reduce_only=bool(reduce_only),
            gold_reserve=Decimal(gold_reserve), foreign_reserve=Decimal(foreign_reserve),
            sell_fee_rate=Decimal(sell_fee_rate),
        )
        fx_pool_versions[pid] = int(pool_version)

    raw = await site_config.get_many(session, [LOAN_DAILY_RATE_KEY, SELL_FEE_RATE_KEY])
    daily_rate = _parse_decimal_or(raw.get(LOAN_DAILY_RATE_KEY), ZERO)
    lmsr_fee_rate = _parse_decimal_or(raw.get(SELL_FEE_RATE_KEY), ZERO)

    groups = tuple(sorted(holdings))
    extra = tuple(sorted(set(extra_groups) - set(groups)))
    snapshots = await _load_snapshots(
        session, list(groups) + list(extra),
        lmsr_fee_rate=lmsr_fee_rate, lmsr_meta=lmsr_meta, fx_meta=fx_meta,
        fx_pool_versions=fx_pool_versions,
    )
    return DependencySet(
        economic_version=economic_version_of(user),
        cash=Decimal(user.cash),
        debt=Decimal(user.debt),
        debt_last_accrued_at=user.debt_last_accrued_at,
        groups=groups,
        holdings={key: dict(value) for key, value in holdings.items()},
        daily_rate=daily_rate,
        lmsr_fee_rate=lmsr_fee_rate,
        snapshots=snapshots,
    )


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

    返回 ``(value, complete)``：``complete=False`` 表示有组缺缓存/缺快照，
    ``value`` 是已完成部分的保守下界（缺的部分按 0）。``cache_only=True``
    时**不做任何报价**（无债快路径），只命中缓存。
    """
    needed = _needed_groups(deps, post)
    snapshots = dict(deps.snapshots) if cache_only else await _ensure_snapshots(session, deps, needed)
    total = ZERO
    complete = True
    for key in needed:
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
            complete = False
            if cache_only:
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
    for key, version in post.base_versions.items():
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


async def _guard_and_refresh_prices(
    session: AsyncSession, *, deps: DependencySet, post: PostTradeState,
) -> DependencySet:
    """R3：锁内重读受影响品种快照，拒绝基于过期价格的模拟后态。

    - 调用方**模拟过价格/状态**的品种（``lmsr_q`` / ``fx_reserves`` /
      ``statuses`` / ``closes_at``）：与 deps 快照或声明的 ``base_versions`` 比对，
      版本前进即抛 ``_StalePostState``（不能拿旧价模拟放行，即使 user 版本没变）。
    - 纯抵押 / 费率类品种：直接刷新快照后重估，不拒绝（持仓数量不依赖价格）。
    """
    # 自有抵押组即使 post 完全没提到也要刷新：外部成交可能已经改了它的 q/储备，
    # 旧引用价会算大/算小 L（复审：wrong-allow）。这类键不在 simulated 集合里，
    # 只做"刷新后重估"，不触发 version_conflict。
    affected = set(deps.groups)
    affected |= set(_market_changed_keys(post)) | set(post.base_versions)
    affected |= set(post.statuses) | set(post.closes_at) | set(post.fee_rates)
    if not affected:
        return deps
    fresh = await _load_snapshots(
        session, sorted(affected), lmsr_fee_rate=deps.lmsr_fee_rate,
    )
    simulated = set(_market_changed_keys(post)) | set(post.statuses) | set(post.closes_at)
    for key in sorted(simulated):
        old_snapshot = deps.snapshots.get(key)
        new_snapshot = fresh.get(key)
        if (old_snapshot is not None and new_snapshot is not None
                and old_snapshot.version != new_snapshot.version):
            raise _StalePostState(f"{key.product}:{key.group_id} 价格/状态已变")
        base = post.base_versions.get(key)
        if (base is not None and new_snapshot is not None
                and tuple(base) != tuple(new_snapshot.version)):
            raise _StalePostState(f"{key.product}:{key.group_id} base_version 已过期")
    merged = dict(deps.snapshots)
    merged.update(fresh)
    return replace(deps, snapshots=merged)


async def _refresh_with_stale_guard(
    session: AsyncSession, *, deps: DependencySet, post: PostTradeState,
) -> Optional[DependencySet]:
    """``_guard_and_refresh_prices`` 的拒绝语义包装：过期模拟价 → None（安全拒绝）。"""
    try:
        return await _guard_and_refresh_prices(session, deps=deps, post=post)
    except _StalePostState as exc:
        logger.warning("risk: 拒绝过期模拟价格（%s）", exc)
        return None


async def _refresh_collateral_snapshots(
    session: AsyncSession, deps: DependencySet,
) -> DependencySet:
    """消费/转出路径：重读全部持仓组快照，避免拿旧抵押价做保证金判断。"""
    if not deps.groups:
        return deps
    fresh = await _load_snapshots(
        session, deps.groups, lmsr_fee_rate=deps.lmsr_fee_rate,
    )
    merged = dict(deps.snapshots)
    merged.update(fresh)
    return replace(deps, snapshots=merged)


async def _revalidate_post(
    session: AsyncSession,
    *,
    user: User,
    user_id: int,
    deps: DependencySet,
    post: PostTradeState,
) -> Optional[tuple[DependencySet, PostTradeState]]:
    """版本复检 + 有界重试；失败返回 None（调用方安全拒绝）。"""
    limit = max(0, int(credit_flags.get_flags().credit_risk_retry_limit))
    attempts = 0
    authoritative = economic_version_of(user)
    while authoritative != deps.economic_version:
        if attempts >= limit:
            return None
        attempts += 1
        fresh = await discover_dependencies(session, user_id)
        if fresh.economic_version == deps.economic_version:
            # 版本没有前进：重试无法收敛（user 行版本与库内快照不一致）
            return None
        if _post_state_stale(deps, fresh, post):
            return None
        post = _rebase_post(deps, fresh, post)
        deps = fresh
    return deps, post


async def _revalidate_cash(
    session: AsyncSession,
    *,
    user: User,
    user_id: int,
    deps: DependencySet,
) -> Optional[DependencySet]:
    """消费路径的版本复检 + 有界重试；失败返回 None。"""
    limit = max(0, int(credit_flags.get_flags().credit_risk_retry_limit))
    attempts = 0
    authoritative = economic_version_of(user)
    while authoritative != deps.economic_version:
        if attempts >= limit:
            return None
        attempts += 1
        fresh = await discover_dependencies(session, user_id)
        if fresh.economic_version == deps.economic_version:
            return None
        deps = fresh
    return deps


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


async def check_new_risk(
    session: AsyncSession,
    *,
    user: User,
    deps: DependencySet,
    post: PostTradeState,
    thresholds: RiskThresholds,
    partial_pct: Decimal,
    now: datetime,
) -> RiskDecision:
    """交易后风险检查（借款 / 买入 / 开仓等增险路径）。

    - 冻结（用户级 / 运营闸）→ 拒绝增险；
    - 版本冲突 → 有界重试 + rebase，超限 `version_conflict`；
    - `D_after == 0` → 直接放行（不报价）；
    - 否则按**全组合**整组清算价值检查初始保证金。
    """
    pct = _as_decimal(partial_pct, "partial_pct")
    uid = _user_id_of(user)
    current = await _current_rates(session, deps)
    if post.debt > ZERO and current.daily_rate != deps.daily_rate:
        return _deny(REASON_VERSION_CONFLICT, cash=post.cash, debt_after=post.debt)
    deps = current
    # R1：版本/冻结/现金一律以数据库权威行为准（刷新 identity map）
    authority = await _refresh_user_authority(session, uid)
    # 无债快路径优先（复审要求）：post 无债时不刷新全组合快照、不报价。
    pre_debt = _effective_debt(
        post.debt, deps.debt_last_accrued_at, deps.daily_rate, now,
    )
    refreshed = False
    if pre_debt > ZERO:
        # R3：锁内重读受影响 + 自有抵押品种快照；模拟价格过期直接拒绝
        guarded = await _refresh_with_stale_guard(session, deps=deps, post=post)
        if guarded is None:
            return _deny(REASON_VERSION_CONFLICT, cash=post.cash, debt_after=pre_debt)
        deps = guarded
        refreshed = True

    revalidated = await _revalidate_post(
        session, user=authority, user_id=uid, deps=deps, post=post,
    )
    if revalidated is None:
        debt_after = _effective_debt(
            post.debt, deps.debt_last_accrued_at, deps.daily_rate, now,
        )
        return _deny(REASON_VERSION_CONFLICT, cash=post.cash, debt_after=debt_after)
    deps, post = revalidated

    reason = _freeze_reason(authority, credit_flags.new_risk_frozen())
    debt_after = _effective_debt(
        post.debt, deps.debt_last_accrued_at, deps.daily_rate, now,
    )
    if reason is not None:
        return _deny(reason, cash=post.cash, debt_after=debt_after)

    if debt_after <= ZERO:
        # 无债快路径：不存在初始保证金约束；不报价，equity/max_borrow 只用缓存值，
        # 缓存不全时 max_borrow=None（需要精确 E 的调用方走 WP2 valuation）。
        holdings_value, complete = await _collateral_value(
            session, user_id=uid, deps=deps, post=post, partial_pct=pct, cache_only=True,
        )
        equity = _q6(post.cash + holdings_value)
        max_borrow = thresholds.max_borrow(equity, ZERO) if complete else None
        return RiskDecision(True, None, equity, ZERO, max_borrow)

    if not refreshed:
        # 重发现后债务才出现（并发新增债务）：此时才刷新全组合 + 过期检查
        guarded = await _refresh_with_stale_guard(session, deps=deps, post=post)
        if guarded is None:
            return _deny(REASON_VERSION_CONFLICT, cash=post.cash, debt_after=debt_after)
        deps = guarded
    holdings_value, _ = await _collateral_value(
        session, user_id=uid, deps=deps, post=post, partial_pct=pct,
    )
    equity = _q6(post.cash + holdings_value - debt_after)
    max_borrow = thresholds.max_borrow(equity, debt_after)
    if equity >= thresholds.r_initial * debt_after:
        return RiskDecision(True, None, equity, debt_after, max_borrow)
    return RiskDecision(
        False, REASON_INSUFFICIENT_INITIAL_MARGIN, equity, debt_after, max_borrow,
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

    持仓与市场态不变（用版本化缓存），只把现金减去 ``spend`` 后重算 E/D。
    """
    amount = _as_decimal(spend, "spend")
    if amount < ZERO:
        raise ValueError(f"spend 不能为负: {amount!r}")
    pct = _as_decimal(partial_pct, "partial_pct")
    uid = _user_id_of(user)
    deps = await _current_rates(session, deps)
    # R1：权威 user 行；R3 同族：重读全部持仓组快照，避免旧抵押价
    authority = await _refresh_user_authority(session, uid)
    # 无债快路径优先：D_after==0 不做全组合快照刷新
    pre_debt = _effective_debt(
        deps.debt, deps.debt_last_accrued_at, deps.daily_rate, now,
    )
    refreshed = False
    if pre_debt > ZERO:
        deps = await _refresh_collateral_snapshots(session, deps)
        refreshed = True

    fresh = await _revalidate_cash(session, user=authority, user_id=uid, deps=deps)
    debt_after = _effective_debt(
        deps.debt, deps.debt_last_accrued_at, deps.daily_rate, now,
    )
    cash_after = deps.cash - amount
    if fresh is None:
        return _deny(REASON_VERSION_CONFLICT, cash=cash_after, debt_after=debt_after)
    deps = fresh
    debt_after = _effective_debt(
        deps.debt, deps.debt_last_accrued_at, deps.daily_rate, now,
    )
    cash_after = deps.cash - amount

    reason = _freeze_reason(authority, credit_flags.new_risk_frozen())
    if reason is not None:
        return _deny(reason, cash=cash_after, debt_after=debt_after)

    post = PostTradeState(cash=cash_after, debt=deps.debt)
    if debt_after <= ZERO:
        holdings_value, complete = await _collateral_value(
            session, user_id=uid, deps=deps, post=post, partial_pct=pct, cache_only=True,
        )
        equity = _q6(cash_after + holdings_value)
        max_borrow = thresholds.max_borrow(equity, ZERO) if complete else None
        return RiskDecision(True, None, equity, ZERO, max_borrow)

    if not refreshed:
        # 重发现后债务才出现：此时才刷新抵押快照再判保证金
        deps = await _refresh_collateral_snapshots(session, deps)
    holdings_value, _ = await _collateral_value(
        session, user_id=uid, deps=deps, post=post, partial_pct=pct,
    )
    equity = _q6(cash_after + holdings_value - debt_after)
    max_borrow = thresholds.max_borrow(equity, debt_after)
    if equity >= thresholds.r_initial * debt_after:
        return RiskDecision(True, None, equity, debt_after, max_borrow)
    return RiskDecision(
        False, REASON_INSUFFICIENT_INITIAL_MARGIN, equity, debt_after, max_borrow,
    )
