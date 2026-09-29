"""WP3 risk：无债快路径、全组合抵押、冻结、版本重试、缓存版本化、交易后持仓。

数据用真实 DB 行（SQLite）；组价值期望与 WP2 `value_user_detailed` 交叉验证。
"""
import os
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import event, select

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.database import async_session_maker, engine
from app.models.base import Market, Outcome, Position, SiteConfig, User
from app.models.fx import FxPair, FxWallet
from app.services.credit import flags as credit_flags
from app.services.credit import risk
from app.services.credit.keys import GroupKey
from app.services.credit.risk import (
    REASON_CREDIT_FROZEN,
    REASON_FROZEN_BY_OPERATOR,
    REASON_INSUFFICIENT_INITIAL_MARGIN,
    REASON_VERSION_CONFLICT,
    PostTradeState,
    check_cash_spend,
    check_new_risk,
    clear_risk_cache,
    discover_dependencies,
)
from app.services.credit.thresholds import derive_thresholds
from app.services.credit.valuation import value_user_detailed

pytestmark = pytest.mark.asyncio

ZERO = Decimal("0")
ONE = Decimal("1")
Q6 = Decimal("0.000001")
RATE = Decimal("0.01")
PCT = Decimal("0.10")
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
TH = derive_thresholds(Decimal("4"), Decimal("0.1"))   # R_initial = 1/3


@pytest.fixture(autouse=True)
def _clean_risk_state():
    credit_flags.clear_flags()
    risk.set_risk_cache_size(risk.DEFAULT_CACHE_SIZE)
    clear_risk_cache()
    yield
    credit_flags.clear_flags()
    clear_risk_cache()
    risk.set_risk_cache_size(risk.DEFAULT_CACHE_SIZE)


# ────────────────────────────── 种子工具 ──────────────────────────────

async def _seed_config(**kv: str) -> None:
    async with async_session_maker() as s:
        async with s.begin():
            for key, value in kv.items():
                s.add(SiteConfig(key=key, value=str(value), value_type="string"))


async def _user(s, name, *, cash="0", debt="0", accrued=None, version=0,
                credit_frozen=False) -> User:
    user = User(
        username=name, cash=Decimal(cash), debt=Decimal(debt),
        debt_last_accrued_at=accrued, economic_version=version,
        credit_frozen=credit_frozen,
    )
    s.add(user)
    await s.commit()
    await s.refresh(user)
    return user


async def _market(s, title, shares, *, status="trading", b=100.0, closes_at=None):
    market = Market(title=title, liquidity_b=b, status=status, closes_at=closes_at)
    s.add(market)
    await s.flush()
    outcomes = [
        Outcome(market_id=market.id, label=f"o{i}", total_shares=Decimal(str(amount)))
        for i, amount in enumerate(shares)
    ]
    s.add_all(outcomes)
    await s.commit()
    for outcome in outcomes:
        await s.refresh(outcome)
    await s.refresh(market)
    return market, outcomes


async def _position(s, user, outcome, amount) -> None:
    s.add(Position(user_id=user.id, outcome_id=outcome.id,
                   amount=Decimal(str(amount)), cost_basis=ZERO))
    await s.commit()


async def _pair(s, code, *, gold="1000", foreign="1000", status="trading",
                reduce_only=False, sell_fee="0") -> FxPair:
    pair = FxPair(
        currency_code=code, currency_name=code, status=status, reduce_only=reduce_only,
        gold_reserve=Decimal(gold), foreign_reserve=Decimal(foreign),
        sell_fee_rate=Decimal(sell_fee), buy_fee_rate=ZERO,
    )
    s.add(pair)
    await s.commit()
    await s.refresh(pair)
    return pair


async def _wallet(s, user, pair, amount) -> None:
    s.add(FxWallet(user_id=user.id, pair_id=pair.id,
                   foreign_amount=Decimal(str(amount)), cost_basis=ZERO))
    await s.commit()


async def _rich_user(s, name, *, cash="500", debt="200", fx_foreign=None):
    """LMSR 持仓 +（可选）FX 钱包的用户。"""
    user = await _user(s, name, cash=cash, debt=debt)
    market, outcomes = await _market(s, f"{name}_m", [100, 100])
    await _position(s, user, outcomes[0], 40)
    pair = None
    if fx_foreign is not None:
        pair = await _pair(s, f"{name}_p", gold="1000", foreign="1000")
        await _wallet(s, user, pair, fx_foreign)
    return user, market, outcomes, pair


async def _deps(uid: int, **kw):
    async with async_session_maker() as s:
        return await discover_dependencies(s, uid, **kw)


async def _check(uid: int, *, post=None, thresholds=TH, deps=None):
    async with async_session_maker() as s:
        user = (await s.execute(
            select(User).where(User.id == uid)
        )).scalars().one()
        current = deps if deps is not None else await discover_dependencies(s, uid)
        if post is None:
            post = PostTradeState(cash=current.cash, debt=current.debt)
        return await check_new_risk(
            s, user=user, deps=current, post=post, thresholds=thresholds,
            partial_pct=PCT, now=NOW,
        )


async def _check_spend(uid: int, spend, *, deps=None, thresholds=TH):
    async with async_session_maker() as s:
        user = (await s.execute(
            select(User).where(User.id == uid)
        )).scalars().one()
        current = deps if deps is not None else await discover_dependencies(s, uid)
        return await check_cash_spend(
            s, user=user, deps=current, spend=Decimal(str(spend)),
            thresholds=thresholds, partial_pct=PCT, now=NOW,
        )


# ────────────────────────────── 发现依赖 ──────────────────────────────

async def test_discover_dependencies_reads_full_collateral_set():
    await _seed_config(loan_daily_rate="0.01", sell_fee_rate="0.02")
    async with async_session_maker() as s:
        user, market, outcomes, pair = await _rich_user(s, "disc", fx_foreign="30")
        uid = int(user.id)
        market_id, outcome_id, pair_id = int(market.id), int(outcomes[0].id), int(pair.id)

    deps = await _deps(uid)
    lmsr_key = GroupKey("lmsr", market_id)
    fx_key = GroupKey("fx", pair_id)
    assert deps.groups == (fx_key, lmsr_key)          # (product, id) 全序：fx < lmsr
    assert deps.holdings[lmsr_key] == {outcome_id: Decimal("40")}
    assert deps.holdings[fx_key] == {pair_id: Decimal("30")}
    assert deps.cash == Decimal("500") and deps.debt == Decimal("200")
    assert deps.daily_rate == Decimal("0.01")
    assert deps.lmsr_fee_rate == Decimal("0.02")
    assert set(deps.snapshots) == {lmsr_key, fx_key}
    lmsr_snapshot = deps.snapshots[lmsr_key]
    assert lmsr_snapshot.b == Decimal("100")
    assert [item.outcome_id for item in lmsr_snapshot.outcomes] == sorted(
        int(item.outcome_id) for item in lmsr_snapshot.outcomes
    )
    assert lmsr_snapshot.version and deps.snapshots[fx_key].version


async def test_discover_extra_groups_prefetches_unheld_market():
    async with async_session_maker() as s:
        user, market, outcomes, _ = await _rich_user(s, "extra")
        other, other_outcomes = await _market(s, "extra_other", [100, 100])
        uid = int(user.id)
        other_key = GroupKey("lmsr", int(other.id))

    deps = await _deps(uid, extra_groups=[other_key])
    assert other_key in deps.snapshots
    assert other_key not in deps.groups


# ────────────────────────────── 估值一致性 ──────────────────────────────

async def test_equity_after_matches_wp2_liquidation_equity_full_set():
    await _seed_config(loan_daily_rate="0.01", sell_fee_rate="0.01")
    async with async_session_maker() as s:
        user, *_ = await _rich_user(s, "consistency", cash="500", debt="200",
                                    fx_foreign="150")
        uid = int(user.id)

    async with async_session_maker() as s:
        deps = await discover_dependencies(s, uid)
        reference = await value_user_detailed(s, uid, daily_rate=RATE)

    decision = await _check(uid, deps=deps, post=PostTradeState(cash=deps.cash, debt=deps.debt))
    # risk 的 E_after 必须用**全组合整组清算价值**，与 WP2 风控口径逐位一致
    assert decision.equity_after == reference.liquidation_equity
    assert decision.allowed is True


async def test_interest_is_included_in_debt_after():
    await _seed_config(loan_daily_rate="0.01")
    async with async_session_maker() as s:
        user = await _user(s, "interest", cash="5000", debt="1000",
                           accrued=NOW - timedelta(days=1))
        uid = int(user.id)
    deps = await _deps(uid)
    decision = await _check(uid, deps=deps)
    # 精确一天：(1.01)^1 → 1010.000000（与 accrue_interest 同源）
    assert decision.debt_after == Decimal("1010.000000")
    assert decision.equity_after == (Decimal("5000") - Decimal("1010")).quantize(Q6)


# ────────────────────────────── 无债快路径 ──────────────────────────────

async def test_no_debt_fastpath_allows_without_any_quote(monkeypatch):
    await _seed_config(loan_daily_rate="0.01")
    async with async_session_maker() as s:
        user, *_ = await _rich_user(s, "nodebt", cash="500", debt="0", fx_foreign="50")
        uid = int(user.id)
    deps = await _deps(uid)
    assert deps.groups  # 有抵押品，但无债

    def _boom(*args, **kwargs):
        raise AssertionError("无债快路径不得做任何组报价")

    monkeypatch.setattr(risk, "quote_lmsr_group", _boom)
    monkeypatch.setattr(risk, "quote_fx_group", _boom)

    decision = await _check(uid, deps=deps)
    assert decision.allowed is True
    assert decision.reason is None
    assert decision.debt_after == ZERO
    # 首次调用没有缓存 → equity 只是保守下界，max_borrow 明确为 None（需精确 E 走 valuation）
    assert decision.max_borrow is None


async def test_no_debt_fastpath_without_collateral_is_exact():
    await _seed_config(loan_daily_rate="0.01")
    async with async_session_maker() as s:
        user = await _user(s, "nodebt_plain", cash="321.5", debt="0")
        uid = int(user.id)
    deps = await _deps(uid)
    assert deps.groups == ()
    decision = await _check(uid, deps=deps)
    assert decision.allowed is True
    assert decision.equity_after == Decimal("321.500000")
    assert decision.max_borrow == TH.max_borrow(Decimal("321.500000"), ZERO)


# ────────────────────────────── 保证金门槛 ──────────────────────────────

async def test_borrow_boundary_allowed_at_limit_rejected_above():
    await _seed_config(loan_daily_rate="0.01")
    async with async_session_maker() as s:
        user, *_ = await _rich_user(s, "boundary", cash="500", debt="100",
                                    fx_foreign="120")
        uid = int(user.id)
    deps = await _deps(uid)
    base = await _check(uid, deps=deps)
    assert base.allowed and base.debt_after == Decimal("100")

    limit = TH.max_borrow(base.equity_after, deps.debt)
    assert limit > ZERO

    at_limit = await _check(uid, deps=deps, post=PostTradeState(
        cash=deps.cash + limit, debt=deps.debt + limit,
    ))
    assert at_limit.allowed is True, at_limit

    above = await _check(uid, deps=deps, post=PostTradeState(
        cash=deps.cash + limit + 1, debt=deps.debt + limit + 1,
    ))
    assert above.allowed is False
    assert above.reason == REASON_INSUFFICIENT_INITIAL_MARGIN


async def test_full_collateral_set_fx_collateral_rescues_lmsr_only_account():
    """同债务/现金/LMSR 持仓下，只有带 FX 抵押的账户能过初始门槛（跨产品合并计算）。"""
    await _seed_config(loan_daily_rate="0.01")
    async with async_session_maker() as s:
        u1, *_ = await _rich_user(s, "coll_a", cash="50", debt="300")
        u2, *_ = await _rich_user(s, "coll_b", cash="50", debt="300", fx_foreign="600")
        uid1, uid2 = int(u1.id), int(u2.id)

    d1 = await _check(uid1)
    d2 = await _check(uid2)
    assert d1.allowed is False and d1.reason == REASON_INSUFFICIENT_INITIAL_MARGIN
    assert d2.allowed is True
    assert d2.equity_after > d1.equity_after


async def test_blocked_group_counts_zero_not_mtm():
    await _seed_config(loan_daily_rate="0.01")
    async with async_session_maker() as s:
        user = await _user(s, "halted", cash="100", debt="50")
        market, outcomes = await _market(s, "halted_m", [100, 100], status="halt")
        await _position(s, user, outcomes[0], 40)
        uid = int(user.id)
    deps = await _deps(uid)
    decision = await _check(uid, deps=deps)
    # 不可交易组 L=0（MTM 不参与风控）
    assert decision.equity_after == (Decimal("100") - Decimal("50")).quantize(Q6)


# ────────────────────────────── 交易后持仓 ──────────────────────────────

async def test_post_holdings_are_valued_instead_of_deps_holdings():
    await _seed_config(loan_daily_rate="0.01")
    async with async_session_maker() as s:
        user, market, outcomes, _ = await _rich_user(s, "posthold", cash="500", debt="200")
        uid, market_id = int(user.id), int(market.id)
    deps = await _deps(uid)
    key = GroupKey("lmsr", market_id)

    unchanged = await _check(uid, deps=deps)
    sold_all = await _check(uid, deps=deps, post=PostTradeState(
        cash=deps.cash + Decimal("10"), debt=deps.debt,   # 清仓回款 10（小于持仓 L）
        post_holdings={key: {}},                          # 实际交易后：已清仓
    ))
    assert sold_all.equity_after == (deps.cash + Decimal("10") - deps.debt).quantize(Q6)
    assert unchanged.equity_after > sold_all.equity_after  # 旧持仓若被复用会高估 E


async def test_post_holdings_new_group_snapshot_loaded_on_demand():
    await _seed_config(loan_daily_rate="0.01")
    async with async_session_maker() as s:
        user, *_ = await _rich_user(s, "newgroup", cash="500", debt="200")
        market2, outcomes2 = await _market(s, "newgroup_m2", [100, 100])
        uid = int(user.id)
        key2 = GroupKey("lmsr", int(market2.id))
        outcome2_id = int(outcomes2[0].id)
    deps = await _deps(uid)
    assert key2 not in deps.snapshots  # 锁外发现时未持有该市场

    base = await _check(uid, deps=deps)
    with_new = await _check(uid, deps=deps, post=PostTradeState(
        cash=deps.cash, debt=deps.debt,
        lmsr_q={key2.group_id: (Decimal("130"), Decimal("100"))},
        post_holdings={key2: {outcome2_id: Decimal("30")}},
    ))
    # 新组在 check 内按需批量补快照并计入 E；不能因为 deps 里没有就漏算
    assert with_new.equity_after > base.equity_after


# ────────────────────────────── 冻结 ──────────────────────────────

async def test_user_credit_frozen_denies_new_risk():
    await _seed_config(loan_daily_rate="0.01")
    async with async_session_maker() as s:
        user, *_ = await _rich_user(s, "frozen_user", cash="500", debt="100")
        uid = int(user.id)
    async with async_session_maker() as s:
        async with s.begin():
            row = (await s.execute(
                select(User).where(User.id == uid)
            )).scalars().one()
            row.credit_frozen = True
    decision = await _check(uid)
    assert decision.allowed is False and decision.reason == REASON_CREDIT_FROZEN


async def test_hot_operator_freeze_takes_effect_without_restart():
    await _seed_config(loan_daily_rate="0.01", credit_new_risk_frozen="true")
    async with async_session_maker() as s:
        user, *_ = await _rich_user(s, "frozen_op", cash="500", debt="100")
        uid = int(user.id)

    frozen = await _check(uid)
    assert frozen.allowed is False and frozen.reason == REASON_FROZEN_BY_OPERATOR

    # 直接在 DB 解冻（不重启、不清 site_config 缓存）→ 下一次判定立即放行
    async with async_session_maker() as s:
        async with s.begin():
            row = (await s.execute(
                select(SiteConfig).where(
                    SiteConfig.key == "credit_new_risk_frozen")
            )).scalars().one()
            row.value = "false"
    thawed = await _check(uid)
    assert thawed.allowed is True


# ────────────────────────────── 版本复检 ──────────────────────────────

async def test_version_conflict_bounded_retry_then_deny(monkeypatch):
    await _seed_config(loan_daily_rate="0.01")
    async with async_session_maker() as s:
        user, *_ = await _rich_user(s, "ver_retry", cash="500", debt="100")
        uid = int(user.id)
    deps = await _deps(uid)
    stale = replace(deps, economic_version=0)

    calls = {"n": 0}

    async def fake_discover(session, user_id, **kwargs):
        calls["n"] += 1
        return replace(deps, economic_version=calls["n"])  # 永远追不上 user 行版本

    monkeypatch.setattr(risk, "discover_dependencies", fake_discover)
    # user 行版本改成 99（锁内权威值），快照版本停在 0
    async with async_session_maker() as s:
        async with s.begin():
            row = (await s.execute(
                select(User).where(User.id == uid)
            )).scalars().one()
            row.economic_version = 99

    decision = await _check(uid, deps=stale, post=PostTradeState(
        cash=stale.cash, debt=stale.debt,
    ))
    assert decision.allowed is False and decision.reason == REASON_VERSION_CONFLICT
    assert calls["n"] == credit_flags.get_flags().credit_risk_retry_limit == 3


async def test_version_rebase_applies_concurrent_cash_delta(monkeypatch):
    """并发入金后重发现依赖：把 delta 叠加到模拟后态，而不是用旧快照拒绝/放行。"""
    await _seed_config(loan_daily_rate="0.01")
    async with async_session_maker() as s:
        user = await _user(s, "rebase", cash="0", debt="0")
        uid = int(user.id)

    async with async_session_maker() as s:            # 锁外发现（cash=0, version=0）
        deps = await discover_dependencies(s, uid)
    assert deps.cash == ZERO and deps.economic_version == 0

    async with async_session_maker() as s:            # 并发入金 +100 并 bump 版本
        async with s.begin():
            row = (await s.execute(
                select(User).where(User.id == uid)
            )).scalars().one()
            row.cash = Decimal("100")
            row.economic_version = 1

    # 相对旧快照借 250：旧快照下 E=0 会被拒；rebase 后 E=100 → 放行
    post = PostTradeState(cash=Decimal("250"), debt=Decimal("250"))
    decision = await _check(uid, deps=deps, post=post)
    assert decision.allowed is True, decision
    assert decision.equity_after == Decimal("100.000000")
    assert decision.debt_after == Decimal("250")


async def test_stale_post_price_version_is_rejected(monkeypatch):
    await _seed_config(loan_daily_rate="0.01")
    async with async_session_maker() as s:
        user, market, outcomes, _ = await _rich_user(s, "stale_post", cash="500", debt="100")
        uid, market_id, outcome_id = int(user.id), int(market.id), int(outcomes[0].id)
        key = GroupKey("lmsr", market_id)

    deps = await _deps(uid)
    stale_post = PostTradeState(
        cash=deps.cash, debt=deps.debt,
        lmsr_q={market_id: (Decimal("140"), Decimal("100"))},
        post_holdings={key: {outcome_id: Decimal("40")}},
        base_versions={key: ("stale-version",)},
    )
    # 并发改 q + bump 用户版本 → post 基于旧价格态，必须拒绝
    async with async_session_maker() as s:
        async with s.begin():
            row = (await s.execute(
                select(Outcome).where(Outcome.id == outcome_id)
            )).scalars().one()
            row.total_shares = Decimal("160")
            urow = (await s.execute(
                select(User).where(User.id == uid)
            )).scalars().one()
            urow.economic_version = 1

    decision = await _check(uid, deps=deps, post=stale_post)
    assert decision.allowed is False and decision.reason == REASON_VERSION_CONFLICT


# ────────────────────────────── 消费 / 转出 ──────────────────────────────

async def test_cash_spend_denied_when_crossing_initial_margin():
    await _seed_config(loan_daily_rate="0.01")
    async with async_session_maker() as s:
        user, *_ = await _rich_user(s, "spend", cash="500", debt="200")
        uid = int(user.id)
    deps = await _deps(uid)

    zero_spend = await _check_spend(uid, "0", deps=deps)
    assert zero_spend.allowed is True
    assert zero_spend.equity_after == (await _check(uid, deps=deps)).equity_after

    denied = await _check_spend(uid, str(zero_spend.equity_after), deps=deps)
    assert denied.allowed is False and denied.reason == REASON_INSUFFICIENT_INITIAL_MARGIN

    with pytest.raises(ValueError):
        await _check_spend(uid, "-1", deps=deps)


async def test_cash_spend_denied_when_frozen():
    await _seed_config(loan_daily_rate="0.01", credit_new_risk_frozen="true")
    async with async_session_maker() as s:
        user, *_ = await _rich_user(s, "spend_frozen", cash="500", debt="100")
        uid = int(user.id)
    decision = await _check_spend(uid, "1")
    assert decision.allowed is False and decision.reason == REASON_FROZEN_BY_OPERATOR


# ────────────────────────────── 缓存 ──────────────────────────────

async def test_group_cache_hits_and_invalidates_on_version_change(monkeypatch):
    await _seed_config(loan_daily_rate="0.01", sell_fee_rate="0.01")
    calls = {"lmsr": 0, "fx": 0}
    original_lmsr = risk.quote_lmsr_group
    original_fx = risk.quote_fx_group

    def counting_lmsr(*args, **kwargs):
        calls["lmsr"] += 1
        return original_lmsr(*args, **kwargs)

    def counting_fx(*args, **kwargs):
        calls["fx"] += 1
        return original_fx(*args, **kwargs)

    monkeypatch.setattr(risk, "quote_lmsr_group", counting_lmsr)
    monkeypatch.setattr(risk, "quote_fx_group", counting_fx)

    async with async_session_maker() as s:
        user, market, outcomes, _ = await _rich_user(
            s, "cache", cash="500", debt="200", fx_foreign="120")
        uid, market_id, outcome_id = int(user.id), int(market.id), int(outcomes[0].id)

    deps = await _deps(uid)
    post = PostTradeState(cash=deps.cash, debt=deps.debt)
    first = await _check(uid, deps=deps, post=post)
    after_first = dict(calls)
    assert after_first == {"lmsr": 1, "fx": 1}
    assert first.allowed is True

    second = await _check(uid, deps=deps, post=post)
    assert calls == after_first                      # 未变组全部命中缓存，不再报价
    assert second.equity_after == first.equity_after
    assert risk.risk_cache_stats().hits >= 2

    # 改 q + bump 用户经济版本 → 快照版本变化 → 缓存必须失效并重算
    async with async_session_maker() as s:
        async with s.begin():
            row = (await s.execute(
                select(Outcome).where(Outcome.id == outcome_id)
            )).scalars().one()
            row.total_shares = Decimal("140")
            urow = (await s.execute(
                select(User).where(User.id == uid)
            )).scalars().one()
            urow.economic_version = int(urow.economic_version or 0) + 1

    fresh = await _deps(uid)
    assert fresh.snapshots[GroupKey("lmsr", market_id)].version != \
        deps.snapshots[GroupKey("lmsr", market_id)].version
    third = await _check(uid, deps=fresh, post=PostTradeState(cash=fresh.cash, debt=fresh.debt))
    assert calls["lmsr"] > after_first["lmsr"]      # 版本变了 → 重新报价
    assert third.equity_after != first.equity_after


async def test_group_cache_is_bounded_lru():
    await _seed_config(loan_daily_rate="0.01", sell_fee_rate="0.01")
    async with async_session_maker() as s:
        user, *_ = await _rich_user(s, "lru", cash="500", debt="200", fx_foreign="120")
        uid = int(user.id)
    deps = await _deps(uid)
    risk.set_risk_cache_size(1)
    await _check(uid, deps=deps)
    stats = risk.risk_cache_stats()
    assert stats.size <= 1
    assert stats.evictions >= 1

# ────────────────────────────── SELECT 上界（批量快照 / 缓存） ──────────────────────────────

def _capture_selects():
    statements: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    return statements, lambda: event.remove(engine.sync_engine, "before_cursor_execute", _capture)


async def test_discover_dependencies_selects_are_bounded():
    """LMSR + FX 全组合快照走固定条数批量 SELECT，与持仓组数无关。"""
    await _seed_config(loan_daily_rate="0.01", sell_fee_rate="0.01")
    async with async_session_maker() as s:
        user, *_ = await _rich_user(s, "sql_bound", cash="500", debt="200", fx_foreign="120")
        market2, outcomes2 = await _market(s, "sql_bound_m2", [100, 100, 100])
        await _position(s, user, outcomes2[1], 25)
        pair2 = await _pair(s, "sql_bound_p2", gold="800", foreign="800")
        await _wallet(s, user, pair2, 40)
        uid = int(user.id)

    from app.services import site_config as site_config_service
    site_config_service.clear_cache()
    statements, stop = _capture_selects()
    try:
        async with async_session_maker() as s:
            deps = await discover_dependencies(s, uid)
    finally:
        stop()
    assert len(deps.groups) == 4                    # 2 LMSR markets + 2 FX pairs
    assert len(statements) <= 6, statements         # User/Position/Outcome/Wallet/site_config


async def test_check_new_risk_selects_bounded_when_cache_warm():
    """未变组命中版本缓存时，check 不应再发估值查询（只有热冻结 1 条 SELECT）。"""
    await _seed_config(loan_daily_rate="0.01", sell_fee_rate="0.01")
    async with async_session_maker() as s:
        user, *_ = await _rich_user(s, "sql_check", cash="500", debt="200", fx_foreign="120")
        uid = int(user.id)
    deps = await _deps(uid)
    post = PostTradeState(cash=deps.cash, debt=deps.debt)
    await _check(uid, deps=deps, post=post)          # 预热缓存

    statements, stop = _capture_selects()
    try:
        decision = await _check(uid, deps=deps, post=post)
    finally:
        stop()
    assert decision.allowed is True
    assert len(statements) <= 2, statements

    # 无债快路径（真实无债用户 + 有抵押）同样不报价、不发额外估值查询
    async with async_session_maker() as s:
        user2, *_ = await _rich_user(s, "sql_check_nodebt", cash="500", debt="0")
        uid2 = int(user2.id)
    deps2 = await _deps(uid2)
    assert deps2.groups and deps2.debt == ZERO
    statements2, stop2 = _capture_selects()
    try:
        fast = await _check(uid2, deps=deps2, post=PostTradeState(cash=deps2.cash, debt=ZERO))
    finally:
        stop2()
    assert fast.allowed is True and fast.debt_after == ZERO
    assert len(statements2) <= 2, statements2
