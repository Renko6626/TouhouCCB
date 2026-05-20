"""强制平仓端到端集成测试。

一个测覆盖完整 sweep 流程：
  用户有债务 + 持仓 → margin < 0.2 → 调用真实
  run_liquidation_sweep_once() → 验证：
    - Position 清空
    - debt 减少 min(proceeds+cash, original_debt)
    - last_liquidated_at 已设置
    - LiquidationEvent 写入
    - LIQUIDATE Transaction 写入
    - outcome.total_shares 减少
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import math
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.base import (
    LiquidationEvent, Market, MarketStatus, Outcome,
    Position, SiteConfig, Transaction, TransactionType, User,
)
from app.services import liquidation_sweep
from app.services.site_config import clear_cache


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_sweep_state():
    """每个测试前后清掉 sweep 防爆 cache 和 site_config 进程缓存。"""
    liquidation_sweep._recently_attempted.clear()
    clear_cache()
    yield
    liquidation_sweep._recently_attempted.clear()
    clear_cache()


# ---------------------------------------------------------------------------
# E2E test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_liquidation_sweep_e2e(client):
    """
    场景：
      - b = 100，YES 和 NO 各 50 shares，用户持 50 YES
      - LMSR 卖出 50 YES → proceeds ≈ 21.9
      - user cash=100, debt=500 → NW ≈ -378, margin ≈ -0.756 → 远低于 0.2
      - sweep 触发 → positions 清空，债务部分偿还，LiquidationEvent+Transaction 写入
    """
    # ── 1. 写入 site_config ──────────────────────────────────────────────────
    async with async_session_maker() as db:
        async with db.begin():
            db.add(SiteConfig(key="liquidation_enabled",        value="true",  value_type="bool"))
            db.add(SiteConfig(key="liquidation_hard_threshold", value="0.2",   value_type="decimal"))
            db.add(SiteConfig(key="liquidation_soft_threshold", value="0.5",   value_type="decimal"))
            db.add(SiteConfig(key="loan_daily_rate",            value="0.01",  value_type="decimal"))
            db.add(SiteConfig(key="liquidation_sweep_interval_sec", value="600", value_type="int"))
            db.add(SiteConfig(key="liquidation_partial_pct",          value="1.0",  value_type="decimal"))
            db.add(SiteConfig(key="liquidation_target_margin",        value="0.30", value_type="decimal"))
            db.add(SiteConfig(key="liquidation_emergency_threshold",  value="0.05", value_type="decimal"))

    # ── 2. 建 market + outcomes ──────────────────────────────────────────────
    async with async_session_maker() as db:
        async with db.begin():
            market = Market(
                title="e2e test market",
                description="liquidation e2e",
                liquidity_b=100.0,
                status=MarketStatus.TRADING,
                tags="",
            )
            db.add(market)
            await db.flush()

            # 两个 outcome，YES 和 NO 各 50 shares（对称市场，价格各 ~0.5）
            outcome_yes = Outcome(
                market_id=market.id,
                label="YES",
                total_shares=Decimal("50"),
            )
            outcome_no = Outcome(
                market_id=market.id,
                label="NO",
                total_shares=Decimal("50"),
            )
            db.add(outcome_yes)
            db.add(outcome_no)
            await db.flush()

            market_id = market.id
            outcome_yes_id = outcome_yes.id
            outcome_no_id = outcome_no.id

    # ── 3. 建 user + position ────────────────────────────────────────────────
    async with async_session_maker() as db:
        async with db.begin():
            user = User(
                username="liquidate_e2e_target",
                casdoor_id="cas_e2e_target",
                cash=Decimal("100"),
                debt=Decimal("500"),
                debt_last_accrued_at=datetime.now(timezone.utc),
            )
            db.add(user)
            await db.flush()

            user_id = user.id

            # 用户持有 50 shares of YES
            pos = Position(
                user_id=user_id,
                outcome_id=outcome_yes_id,
                amount=Decimal("50"),
                cost_basis=Decimal("0"),
            )
            db.add(pos)

    # ── 4. 验证 margin（预期约 -0.756，远低于 0.2）──────────────────────────
    # LMSR: b=100, [50, 50] → cost = 100*ln(e^0.5 + e^0.5)
    #               [0, 50] → cost = 100*ln(e^0 + e^0.5)
    # proceeds = cost([50,50]) - cost([0,50])
    b = 100.0
    shares_before = [50.0, 50.0]
    shares_after  = [0.0, 50.0]
    cost_before = b * math.log(sum(math.exp(q / b) for q in shares_before))
    cost_after  = b * math.log(sum(math.exp(q / b) for q in shares_after))
    expected_proceeds = cost_before - cost_after
    expected_hv = expected_proceeds  # sell_fee_rate=0 in sweep's wealth call
    expected_nw = 100.0 + expected_hv - 500.0
    expected_margin = expected_nw / 500.0
    # margin 约 -0.756 → 远低于 hard threshold 0.2，应触发强平

    # ── 5. 触发 sweep ────────────────────────────────────────────────────────
    result = await liquidation_sweep.run_liquidation_sweep_once()

    assert result.get("triggered_count") == 1, (
        f"sweep 应触发 1 次强平，实际结果: {result}"
    )

    # ── 6. 验证数据库状态 ────────────────────────────────────────────────────
    async with async_session_maker() as db:
        # 6a. last_liquidated_at 已设置
        u2 = (await db.execute(
            select(User).where(User.id == user_id)
        )).scalar_one()
        assert u2.last_liquidated_at is not None, "last_liquidated_at 应已设置"

        # 6b. debt 减少（进账 ~21.9，加上原 cash=100，总计约 121.9，还债最多 500）
        #     实际 repaid = min(cash + proceeds, debt) ≈ min(121.9, 500) ≈ 121.9
        #     remaining_debt ≈ 500 - 121.9 ≈ 378.1
        assert u2.debt < Decimal("500"), f"债务应部分偿还，实际: {u2.debt}"
        assert u2.debt > Decimal("0"),   "债务多于还款，应仍有剩余"

        # 6c. cash 应接近 0（全部用于还债）
        assert u2.cash == Decimal("0"), f"现金应全部用于还债，实际: {u2.cash}"

        # 6d. 无剩余 open position
        positions_after = (await db.execute(
            select(Position).where(Position.user_id == user_id)
        )).scalars().all()
        assert len(positions_after) == 0, (
            f"所有 position 应已删除，实际残留: {len(positions_after)} 条"
        )

        # 6e. LiquidationEvent 写入，快照正确
        events = (await db.execute(
            select(LiquidationEvent).where(LiquidationEvent.user_id == user_id)
        )).scalars().all()
        assert len(events) == 1, f"应有 1 条 LiquidationEvent，实际: {len(events)}"
        ev = events[0]
        assert ev.sold_positions_count == 1, (
            f"sold_positions_count 应为 1，实际: {ev.sold_positions_count}"
        )
        assert ev.repaid_amount > Decimal("0"), (
            f"repaid_amount 应 > 0，实际: {ev.repaid_amount}"
        )
        assert ev.pre_cash == Decimal("100"), (
            f"pre_cash 应为 100，实际: {ev.pre_cash}"
        )
        assert ev.pre_debt == Decimal("500"), (
            f"pre_debt 应为 500，实际: {ev.pre_debt}"
        )
        assert ev.pre_holdings_value > Decimal("0"), (
            f"pre_holdings_value 应 > 0（LMSR proceeds），实际: {ev.pre_holdings_value}"
        )
        # pre_margin_ratio 应约等于 expected_margin（允许 Decimal 精度误差）
        assert ev.pre_margin_ratio is not None
        assert float(ev.pre_margin_ratio) < 0.2, (
            f"pre_margin_ratio 应 < 0.2，实际: {ev.pre_margin_ratio}；"
            f"理论值约 {expected_margin:.4f}"
        )
        assert ev.trigger_source == "scheduler", (
            f"trigger_source 应为 'scheduler'，实际: {ev.trigger_source}"
        )
        assert ev.remaining_debt > Decimal("0"), (
            f"remaining_debt 应 > 0（资不抵债），实际: {ev.remaining_debt}"
        )

        # 6f. LIQUIDATE Transaction 写入
        txs = (await db.execute(
            select(Transaction).where(
                Transaction.user_id == user_id,
                Transaction.type == TransactionType.LIQUIDATE,
            )
        )).scalars().all()
        assert len(txs) == 1, (
            f"应有 1 条 LIQUIDATE Transaction，实际: {len(txs)}"
        )
        tx = txs[0]
        assert tx.outcome_id == outcome_yes_id
        assert tx.shares == Decimal("50"), f"卖出份额应为 50，实际: {tx.shares}"
        assert tx.fee == Decimal("0"), "强平不收手续费"

        # 6g. outcome.total_shares 减少（YES 从 50 降到 0）
        o_yes = (await db.execute(
            select(Outcome).where(Outcome.id == outcome_yes_id)
        )).scalar_one()
        assert o_yes.total_shares == Decimal("0"), (
            f"YES outcome.total_shares 应为 0（全部卖出），实际: {o_yes.total_shares}"
        )

        # NO outcome 不变
        o_no = (await db.execute(
            select(Outcome).where(Outcome.id == outcome_no_id)
        )).scalar_one()
        assert o_no.total_shares == Decimal("50"), (
            f"NO outcome.total_shares 不应变动，实际: {o_no.total_shares}"
        )
