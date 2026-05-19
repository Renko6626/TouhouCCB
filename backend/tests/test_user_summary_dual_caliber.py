"""/user/summary + /user/holdings 双口径（MTM/LCV）端点级测试。

补 review #3 gap：之前只在 service 层（test_wealth_mtm.py）测过批量计算函数，
没有端点级验证字段完整、跨字段不变量、HALT 行表格 ≡ summary。
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from decimal import Decimal

import pytest

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import (
    Market, MarketStatus, Outcome, Position, User,
)


async def _make_user_with_position(
    *, cash=Decimal("100"), amount=Decimal("100"),
    cost_basis=Decimal("50"), market_status=MarketStatus.TRADING,
):
    """造 1 个用户 + 1 个 2-outcome 市场 + 用户在 outcome[0] 的持仓。
    返回 (user_id, auth_header, market_id)."""
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(
                username=f"dc_{suffix}",
                email=f"{suffix}@t.com",
                casdoor_id=f"cd_{suffix}",
                cash=cash,
                debt=Decimal("0"),
            )
            s.add(u)
            await s.flush()

            m = Market(
                title=f"m_{suffix}",
                description="dual-caliber-test",
                liquidity_b=100.0,
                status=market_status,
                tags="",
            )
            s.add(m)
            await s.flush()

            o0 = Outcome(market_id=m.id, label="A", total_shares=amount)
            o1 = Outcome(market_id=m.id, label="B", total_shares=Decimal("0"))
            s.add_all([o0, o1])
            await s.flush()

            p = Position(
                user_id=u.id, outcome_id=o0.id, amount=amount,
                cost_basis=cost_basis,
            )
            s.add(p)
    token = create_access_token(u.id)
    return u.id, {"Authorization": f"Bearer {token}"}, m.id


@pytest.mark.asyncio
async def test_summary_returns_dual_caliber_fields(client):
    """/user/summary 必须返回 MTM 和 LCV 两套字段。"""
    _, h, _ = await _make_user_with_position(amount=Decimal("200"))
    r = await client.get("/api/v1/user/summary", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()

    # MTM 主显示
    assert "holdings_value" in body
    assert "net_worth" in body
    assert "unrealized_pnl" in body
    # LCV 副显示
    assert "holdings_value_liquidation" in body
    assert "net_worth_liquidation" in body
    assert "unrealized_pnl_liquidation" in body


@pytest.mark.asyncio
async def test_summary_mtm_strictly_greater_than_lcv(client):
    """非平凡持仓 MTM > LCV（含 LMSR 滑点 + sell_fee 损耗）。"""
    _, h, _ = await _make_user_with_position(amount=Decimal("200"))
    r = await client.get("/api/v1/user/summary", headers=h)
    body = r.json()
    assert float(body["holdings_value"]) > float(body["holdings_value_liquidation"]), (
        f"holdings_value (MTM) {body['holdings_value']} 应 > "
        f"holdings_value_liquidation (LCV) {body['holdings_value_liquidation']}"
    )
    # 净值同样关系（因为 cash/debt 一样，差只来自 holdings）
    assert float(body["net_worth"]) > float(body["net_worth_liquidation"])


@pytest.mark.asyncio
async def test_holdings_row_returns_dual_caliber_fields(client):
    """/user/holdings 每行也要有 unrealized_pnl + unrealized_pnl_liquidation。"""
    _, h, _ = await _make_user_with_position(amount=Decimal("200"))
    r = await client.get("/api/v1/user/holdings", headers=h)
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1
    row = rows[0]
    assert "unrealized_pnl" in row
    assert "unrealized_pnl_liquidation" in row


@pytest.mark.asyncio
async def test_holdings_row_sum_equals_summary_trading(client):
    """关键不变量：Σ holdings.unrealized_pnl ≈ summary.unrealized_pnl（含 TRADING 持仓）。

    防止顶部资产卡 ≠ 表格底部求和的体感分裂（这次分支的核心目标）。
    """
    _, h, _ = await _make_user_with_position(amount=Decimal("150"))
    summary = (await client.get("/api/v1/user/summary", headers=h)).json()
    rows = (await client.get("/api/v1/user/holdings", headers=h)).json()

    sum_mtm = sum(float(row["unrealized_pnl"]) for row in rows)
    sum_lcv = sum(float(row["unrealized_pnl_liquidation"]) for row in rows)

    # 0.01 量化级别的小差距可接受（per-position quantize vs sum-then-quantize）
    assert abs(sum_mtm - float(summary["unrealized_pnl"])) < 0.05, (
        f"MTM 求和 {sum_mtm} 应 ≈ summary.unrealized_pnl "
        f"{summary['unrealized_pnl']}"
    )
    assert abs(sum_lcv - float(summary["unrealized_pnl_liquidation"])) < 0.05


@pytest.mark.asyncio
async def test_halt_position_mtm_uses_marginal_price_but_lcv_is_negative_cost(client):
    """HALT 行：
    - MTM 浮盈仍按瞬时价算（账面估值不归零，避免临时 HALT 让用户慌）
    - LCV 浮盈 = -cost_basis（不可变现 → 立即清算价值为 0）
    - market_value 置 0 表达"现在卖不出"
    """
    _, h, _ = await _make_user_with_position(
        amount=Decimal("100"),
        cost_basis=Decimal("40"),
        market_status=MarketStatus.HALT,
    )
    rows = (await client.get("/api/v1/user/holdings", headers=h)).json()
    assert len(rows) == 1
    row = rows[0]

    # MTM 浮盈 = amount × price - cost。amount=100, market shares=100, b=100
    # → price ≈ 0.7311, MTM ≈ 73.11, pnl ≈ 33.11
    assert float(row["unrealized_pnl"]) > 0, (
        f"HALT 行 MTM 浮盈应按瞬时价正常算，不是 -cost_basis，实拿 "
        f"{row['unrealized_pnl']}"
    )
    # LCV 浮盈 = 0 - cost_basis = -40
    assert float(row["unrealized_pnl_liquidation"]) == pytest.approx(-40.0, abs=0.01)
    assert float(row["market_value"]) == pytest.approx(0.0, abs=0.01)


@pytest.mark.asyncio
async def test_halt_row_sum_equals_summary(client):
    """HALT 场景下表格求和仍应等 summary（review #1 阻塞 issue 的核心断言）。

    MTM 两边都算 HALT（自动一致）；LCV 两边都过滤 HALT（HALT 行 = -cost，
    summary 也是 0 holdings - cost_basis）。
    """
    _, h, _ = await _make_user_with_position(
        amount=Decimal("100"),
        cost_basis=Decimal("40"),
        market_status=MarketStatus.HALT,
    )
    summary = (await client.get("/api/v1/user/summary", headers=h)).json()
    rows = (await client.get("/api/v1/user/holdings", headers=h)).json()

    sum_mtm = sum(float(row["unrealized_pnl"]) for row in rows)
    sum_lcv = sum(float(row["unrealized_pnl_liquidation"]) for row in rows)

    assert abs(sum_mtm - float(summary["unrealized_pnl"])) < 0.05
    assert abs(sum_lcv - float(summary["unrealized_pnl_liquidation"])) < 0.05


@pytest.mark.asyncio
async def test_thresholds_always_in_summary(client):
    """margin_hard_threshold + margin_soft_threshold 不论 debt 是否 > 0 都返回。"""
    _, h, _ = await _make_user_with_position()  # debt=0
    body = (await client.get("/api/v1/user/summary", headers=h)).json()
    assert "margin_hard_threshold" in body
    assert "margin_soft_threshold" in body
    # site_config 没 seed 时 fallback 默认
    assert float(body["margin_hard_threshold"]) == 0.2
    assert float(body["margin_soft_threshold"]) == 0.5
