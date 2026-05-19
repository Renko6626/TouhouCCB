"""Leaderboard net_worth 模式：验证含持仓 LMSR 清算价排序，与 /user/summary 同口径。

回归预防：曾经按 cash-debt 排序（忽略持仓），导致满仓玩家被现金囤积者反超。
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import math
import uuid
from decimal import Decimal

import pytest

from app.core.database import async_session_maker
from app.models.base import Market, MarketStatus, Outcome, Position, User


async def _seed_user(*, cash: Decimal, debt: Decimal = Decimal("0")) -> tuple[int, str]:
    suffix = uuid.uuid4().hex[:8]
    username = f"u_{suffix}"
    async with async_session_maker() as s:
        u = User(
            username=username,
            email=f"{suffix}@t.com",
            casdoor_id=f"cd_{suffix}",
            cash=cash,
            debt=debt,
            is_superuser=False,
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return u.id, username


async def _seed_market_with_position(
    *,
    user_id: int,
    user_shares: Decimal,
    liquidity_b: float = 100.0,
) -> int:
    """造一个 2-outcome 市场，user 持有 outcome[0] 的全部 user_shares；
    outcome[0].total_shares = user_shares（与持仓一致）。"""
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        m = Market(
            title=f"m_{suffix}",
            description="leaderboard-test",
            liquidity_b=liquidity_b,
            status=MarketStatus.TRADING,
        )
        s.add(m)
        await s.flush()
        outcomes = []
        for i in range(2):
            o = Outcome(
                market_id=m.id,
                label=f"opt_{i}",
                total_shares=user_shares if i == 0 else Decimal("0"),
            )
            s.add(o)
            await s.flush()
            outcomes.append(o.id)
        pos = Position(
            user_id=user_id,
            outcome_id=outcomes[0],
            amount=user_shares,
            cost_basis=Decimal("0"),
        )
        s.add(pos)
        await s.commit()
        return m.id


def _expected_mtm(shares: float, b: float) -> float:
    """outcome[0] 持有 shares、outcome[1]=0、b=100 时的 MTM = shares × marginal_price。

    leaderboard 现在用 MTM 排序口径（瞬时价 × 数量，不含滑点）。
    详见 docs/holdings-value-semantics.md。
    """
    exp0 = math.exp(shares / b)
    exp1 = 1.0
    price_0 = exp0 / (exp0 + exp1)
    return shares * price_0


@pytest.mark.asyncio
async def test_leaderboard_includes_holdings_value(client):
    """核心断言：持仓重的玩家应排在现金更多但无持仓的玩家前面。"""
    # whale: cash=10, 持有 200 share，MTM 估值约 200 × 0.88 ≈ 176 → NW ≈ 186
    whale_id, whale_name = await _seed_user(cash=Decimal("10"))
    await _seed_market_with_position(user_id=whale_id, user_shares=Decimal("200"))

    # cash_only: cash=100, 无持仓 → net_worth = 100
    _, cash_only_name = await _seed_user(cash=Decimal("100"))

    # 校验 setup 落在 whale 应该胜出的区间
    expected_whale_holdings = _expected_mtm(200.0, 100.0)
    assert expected_whale_holdings > 90, f"setup 失误：whale 持仓估值 {expected_whale_holdings} 不足以超过 cash_only"

    resp = await client.get("/api/v1/market/leaderboard", params={"mode": "net_worth", "limit": 100})
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    names = [r["username"] for r in rows]
    assert whale_name in names and cash_only_name in names

    whale_pos = names.index(whale_name)
    cash_only_pos = names.index(cash_only_name)
    assert whale_pos < cash_only_pos, (
        f"whale 应排在 cash_only 之前；当前 names={names}"
    )

    # whale 的 net_worth 应 ≈ 10 + MTM(176) ≈ 186
    whale_nw = float(rows[whale_pos]["net_worth"])
    assert whale_nw > 100, f"whale net_worth={whale_nw}，未含持仓估值"
    # 允许 0.5 单位误差（量化到 0.01，且 LMSR float 计算）
    assert abs(whale_nw - (10.0 + expected_whale_holdings)) < 0.5


@pytest.mark.asyncio
async def test_leaderboard_no_holdings_falls_back_to_cash_minus_debt(client):
    """无持仓用户：net_worth 仍 = cash - debt。"""
    _, plain_name = await _seed_user(cash=Decimal("250"), debt=Decimal("50"))

    resp = await client.get("/api/v1/market/leaderboard", params={"mode": "net_worth", "limit": 100})
    assert resp.status_code == 200
    rows = resp.json()
    by_name = {r["username"]: r for r in rows}
    assert plain_name in by_name
    assert float(by_name[plain_name]["net_worth"]) == 200.0


@pytest.mark.asyncio
async def test_leaderboard_limit_respected(client):
    """limit=N 只返回 top-N。"""
    for i in range(5):
        await _seed_user(cash=Decimal(str(100 + i * 10)))

    resp = await client.get("/api/v1/market/leaderboard", params={"mode": "net_worth", "limit": 2})
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) <= 2
    # 严格降序
    nws = [float(r["net_worth"]) for r in rows]
    assert nws == sorted(nws, reverse=True)
