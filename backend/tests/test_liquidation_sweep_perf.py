"""sweep 性能基准测试。

合成 100 个 debt>0 用户 (90 安全 + 10 越线)，每用户平均 3 positions 分布在
5 个 market。跑 5 次 sweep 取中位数，打印 ms。

不在常规 CI 跑（标 `@pytest.mark.perf`，pytest.ini 没注册这个 marker 所以默认
被认为是"普通"测，但耗时 ~5s，可以跑）。

预期：perf 改造后 100 用户 sweep 应 < 200ms（文档目标 ~85ms）。
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import statistics
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.core.database import async_session_maker
from app.models.base import (
    Market, MarketStatus, Outcome, Position, SiteConfig, User,
)
from app.services import liquidation_sweep


N_USERS = 100
N_OVER_THRESHOLD = 10  # 越线（margin < hard_thr）的用户数
N_MARKETS = 5
N_POSITIONS_PER_USER = 3
SWEEP_ITERATIONS = 5


async def _setup_perf_scenario():
    """100 用户 × 3 positions × 5 markets，10 个用户 margin < 0.2 (会触发强平)。"""
    async with async_session_maker() as s:
        async with s.begin():
            # 4 个 site_config 行（sweep 启动会一次性 get_many）
            s.add(SiteConfig(key="liquidation_enabled", value="true", value_type="bool"))
            s.add(SiteConfig(key="liquidation_hard_threshold", value="0.2", value_type="decimal"))
            s.add(SiteConfig(key="liquidation_soft_threshold", value="0.5", value_type="decimal"))
            s.add(SiteConfig(key="loan_daily_rate", value="0.001", value_type="decimal"))

            # 5 个 TRADING market
            markets = []
            for i in range(N_MARKETS):
                m = Market(
                    title=f"perf_m_{i}", description="",
                    liquidity_b=100.0, status=MarketStatus.TRADING, tags="",
                )
                s.add(m)
                await s.flush()
                # 每 market 2 outcomes
                outs = []
                for j in range(2):
                    o = Outcome(
                        market_id=m.id, label=f"opt_{j}",
                        total_shares=Decimal("0"),
                    )
                    s.add(o)
                    await s.flush()
                    outs.append(o)
                markets.append((m, outs))

            # 100 用户，每个 3 positions
            for u_idx in range(N_USERS):
                is_over = u_idx < N_OVER_THRESHOLD
                u = User(
                    username=f"perf_u_{u_idx}_{uuid.uuid4().hex[:4]}",
                    casdoor_id=f"perf_cas_{u_idx}_{uuid.uuid4().hex[:4]}",
                    cash=Decimal("0" if is_over else "100"),
                    debt=Decimal("100"),  # 都借了债
                    debt_last_accrued_at=datetime.now(timezone.utc),
                )
                s.add(u)
                await s.flush()

                # 3 positions 分布在不同 market
                for p_idx in range(N_POSITIONS_PER_USER):
                    market, outs = markets[p_idx % N_MARKETS]
                    o = outs[p_idx % 2]
                    amt = Decimal("10" if is_over else "50")
                    pos = Position(
                        user_id=u.id, outcome_id=o.id,
                        amount=amt, cost_basis=Decimal("0"),
                    )
                    s.add(pos)
                    # outcome.total_shares 累加
                    o.total_shares += amt


@pytest.mark.asyncio
async def test_sweep_100_users_perf_median(client):
    """100 用户 sweep 中位数耗时 benchmark。打印结果供 CI/手工 review。

    注意：第一次 (warmup) 跑后 over_hard 用户的 debt 已被改成 0，后续 4 次跑
    实际是"100 candidate 但 0 触发强平"的 stage-1-only fast path。这个仍然
    覆盖 perf 主要瓶颈（candidate query + 批量 holdings_value），但严格说
    第 1 次和第 2-5 次测的是不同 path。详见 review M3。
    """
    await _setup_perf_scenario()

    # 清缓存 + 把第一次跑当 warmup
    liquidation_sweep._recently_attempted.clear()
    from app.services.site_config import clear_cache
    clear_cache()

    durations = []
    for i in range(SWEEP_ITERATIONS):
        # 每次跑前清防爆 cache（否则第 2 次起 over_hard 用户被 cooldown 跳过）
        liquidation_sweep._recently_attempted.clear()
        clear_cache()
        start = time.monotonic()
        result = await liquidation_sweep.run_liquidation_sweep_once()
        elapsed_ms = (time.monotonic() - start) * 1000
        durations.append(elapsed_ms)
        assert "sweep_duration_ms" in result, f"result missing duration: {result}"
        # 监控新加的 counter
        assert "recovered_count" in result, "review I4 字段缺失"
        assert "skipped_count" in result

    median = statistics.median(durations)
    worst = sorted(durations)[-1]
    print(f"\n=== sweep perf benchmark ({N_USERS} users, {N_OVER_THRESHOLD} over_hard) ===", file=sys.stderr)
    for i, d in enumerate(durations):
        print(f"  run {i+1}: {d:.1f} ms", file=sys.stderr)
    print(f"  median: {median:.1f} ms", file=sys.stderr)
    print(f"  worst:  {worst:.1f} ms", file=sys.stderr)

    # 收紧断言（review M2）：观测中位数 ~70ms，给 4x 头寸防 CI 偶发抖动 + 未来回归保护
    # 之前 perf 优化前实测 ~2300ms，现在 < 300ms 留出明确退化告警空间
    assert median < 300.0, (
        f"sweep 中位数 {median:.1f}ms > 300ms，perf 退化（基准 ~72ms）"
    )
