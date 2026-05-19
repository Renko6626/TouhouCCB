"""compute_users_holdings_value_mtm 直接单元测。

补 review 时发现 MTM 函数只通过 leaderboard 测试间接覆盖的 gap。
重点验证：边界态 + HALT 市场过滤 + MTM > LCV 不变量。
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import math
import uuid
from decimal import Decimal

import pytest

from app.core.database import async_session_maker
from app.models.base import Market, MarketStatus, Outcome, Position, User
from app.services.wealth import (
    compute_users_holdings_value,
    compute_users_holdings_value_mtm,
)


async def _seed_user(*, cash: Decimal = Decimal("100")) -> int:
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as s:
        u = User(
            username=f"mtm_{suffix}",
            email=f"{suffix}@t.com",
            casdoor_id=f"cd_{suffix}",
            cash=cash,
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return u.id


async def _seed_market_position(
    *,
    user_id: int,
    amount: Decimal,
    status: MarketStatus = MarketStatus.TRADING,
    liquidity_b: float = 100.0,
) -> int:
    """单 outcome=amount, 另一支=0 的 2-outcome 市场。返回 market_id。"""
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        m = Market(
            title=f"m_{suffix}",
            description="mtm-test",
            liquidity_b=liquidity_b,
            status=status,
            tags="",
        )
        s.add(m)
        await s.flush()
        o0 = Outcome(market_id=m.id, label="A", total_shares=amount)
        o1 = Outcome(market_id=m.id, label="B", total_shares=Decimal("0"))
        s.add_all([o0, o1])
        await s.flush()
        p = Position(
            user_id=user_id, outcome_id=o0.id, amount=amount,
            cost_basis=Decimal("0"),
        )
        s.add(p)
        await s.commit()
        return m.id


@pytest.mark.asyncio
async def test_mtm_empty_user_ids_returns_empty_dict():
    async with async_session_maker() as s:
        result = await compute_users_holdings_value_mtm(s, user_ids=[])
    assert result == {}


@pytest.mark.asyncio
async def test_mtm_user_without_positions_returns_empty():
    uid = await _seed_user()
    async with async_session_maker() as s:
        result = await compute_users_holdings_value_mtm(s, user_ids=[uid])
    # 用户 ID 不在结果中（compute_* 系列只返回有持仓的用户）
    assert uid not in result


@pytest.mark.asyncio
async def test_mtm_matches_expected_formula():
    """单持仓场景：MTM = amount × marginal_price，验证数学公式。"""
    uid = await _seed_user()
    await _seed_market_position(user_id=uid, amount=Decimal("200"))

    async with async_session_maker() as s:
        result = await compute_users_holdings_value_mtm(s, user_ids=[uid])

    # b=100, outcome[0]=200, outcome[1]=0
    # price_0 = e^2 / (e^2 + 1) ≈ 0.8807970779
    # MTM ≈ 200 × 0.8807970779 ≈ 176.16
    expected = 200.0 * (math.exp(2.0) / (math.exp(2.0) + 1.0))
    assert abs(float(result[uid]) - expected) < 0.01


@pytest.mark.asyncio
async def test_mtm_includes_halt_markets():
    """MTM 不过滤 HALT：账面估值按瞬时价算，避免临时 HALT 让用户账面归零。

    LCV 那边过滤 HALT 是为了 margin/借款的保守语义，两套口径职责不同。
    """
    uid = await _seed_user()
    await _seed_market_position(
        user_id=uid, amount=Decimal("200"),
        status=MarketStatus.HALT,
    )

    async with async_session_maker() as s:
        result = await compute_users_holdings_value_mtm(s, user_ids=[uid])

    # MTM 应该算 HALT 持仓的瞬时价 × 数量 ≈ 176
    assert result.get(uid, Decimal("0")) > Decimal("100"), (
        f"HALT 持仓应正常计入 MTM（账面价值），拿到 {result.get(uid)}"
    )


@pytest.mark.asyncio
async def test_lcv_skips_halt_markets():
    """LCV 过滤 HALT：不可变现 → 立即清算价值为 0。防 fake collateral。"""
    uid = await _seed_user()
    await _seed_market_position(
        user_id=uid, amount=Decimal("200"),
        status=MarketStatus.HALT,
    )

    async with async_session_maker() as s:
        result = await compute_users_holdings_value(s, user_ids=[uid])

    assert result.get(uid, Decimal("0")) == Decimal("0")


@pytest.mark.asyncio
async def test_mtm_strictly_greater_than_lcv():
    """非平凡持仓上 MTM > LCV（滑点 + sell_fee 让 LCV 偏低）。"""
    uid = await _seed_user()
    await _seed_market_position(user_id=uid, amount=Decimal("200"))

    async with async_session_maker() as s:
        mtm = await compute_users_holdings_value_mtm(s, user_ids=[uid])
        lcv = await compute_users_holdings_value(s, user_ids=[uid])

    assert mtm[uid] > lcv[uid], (
        f"MTM {mtm[uid]} 应 > LCV {lcv[uid]}（含滑点 + sell_fee）"
    )
    # 差距应该相当显著（200 股 / b=100，滑点 ~20%）
    gap = float(mtm[uid]) - float(lcv[uid])
    assert gap > 20.0, f"MTM-LCV 差距 {gap} 不符合 LMSR 滑点预期"


@pytest.mark.asyncio
async def test_mtm_aggregates_across_multiple_markets():
    """跨 market 持仓应累加。"""
    uid = await _seed_user()
    await _seed_market_position(user_id=uid, amount=Decimal("50"))
    await _seed_market_position(user_id=uid, amount=Decimal("80"))

    async with async_session_maker() as s:
        result = await compute_users_holdings_value_mtm(s, user_ids=[uid])

    # 50 股: price = e^0.5/(e^0.5+1) ≈ 0.6225, MTM ≈ 31.12
    # 80 股: price = e^0.8/(e^0.8+1) ≈ 0.6900, MTM ≈ 55.20
    # 合计 ≈ 86.3
    p1 = math.exp(0.5) / (math.exp(0.5) + 1.0)
    p2 = math.exp(0.8) / (math.exp(0.8) + 1.0)
    expected = 50.0 * p1 + 80.0 * p2
    assert abs(float(result[uid]) - expected) < 0.5


@pytest.mark.asyncio
async def test_mtm_returns_only_requested_users():
    """user_ids 过滤生效：A 有持仓但不在 user_ids → 不返回。"""
    uid_a = await _seed_user()
    uid_b = await _seed_user()
    await _seed_market_position(user_id=uid_a, amount=Decimal("100"))
    await _seed_market_position(user_id=uid_b, amount=Decimal("100"))

    async with async_session_maker() as s:
        result = await compute_users_holdings_value_mtm(s, user_ids=[uid_b])

    assert uid_a not in result
    assert uid_b in result
