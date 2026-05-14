"""
P1 修复回归测试：
1. TradeRequest 新字段 max_cost / min_proceeds / max_slippage_bps 验证
2. BUY/SELL 路径滑点拒绝 + 默认兜底
3. SELL 锁顺序源码级 assert（防止未来 PR 静默回退）
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import re
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import Market, MarketStatus, Outcome, User
from app.schemas.market import TradeRequest


async def _make_user(cash=Decimal("10000")):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(
                username=f"u_{suffix}",
                email=f"{suffix}@t.com",
                casdoor_id=f"cd_{suffix}",
                cash=cash,
                debt=Decimal("0"),
                is_superuser=False,
            )
            s.add(u)
            await s.flush()
            uid = u.id
    token = create_access_token(uid)
    return uid, {"Authorization": f"Bearer {token}"}


async def _make_market(liquidity_b: float = 10.0, n_outcomes: int = 2):
    """
    创建一个 b=10、双结果的小流动性市场。
    LMSR 在 b=10 下 buy 5 份会产生 ~12.6% 滑点，足以触发 hardcap。
    """
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            m = Market(
                title=f"m_{suffix}",
                description="slippage-test",
                liquidity_b=liquidity_b,
                status=MarketStatus.TRADING,
            )
            s.add(m)
            await s.flush()
            outcome_ids = []
            for i in range(n_outcomes):
                o = Outcome(market_id=m.id, label=f"opt_{i}", total_shares=Decimal("0"))
                s.add(o)
                await s.flush()
                outcome_ids.append(o.id)
            mid = m.id
    return mid, outcome_ids


# ────────────────────────────────────────────────
# Schema 验证
# ────────────────────────────────────────────────

def test_trade_request_accepts_new_fields():
    """新字段可被接受、为 Optional、有 validation。"""
    # 全部省略 → 字段都是 None
    req = TradeRequest(outcome_id=1, shares=Decimal("1"))
    assert req.max_cost is None
    assert req.min_proceeds is None
    assert req.max_slippage_bps is None

    # 全部提供，正常解析
    req = TradeRequest(
        outcome_id=1,
        shares=Decimal("1"),
        max_cost=Decimal("100"),
        min_proceeds=Decimal("50"),
        max_slippage_bps=200,
    )
    assert req.max_cost == Decimal("100")
    assert req.min_proceeds == Decimal("50")
    assert req.max_slippage_bps == 200


def test_trade_request_rejects_invalid_bps():
    """bps 上下限 0~10000。"""
    with pytest.raises(Exception):
        TradeRequest(outcome_id=1, shares=Decimal("1"), max_slippage_bps=-1)
    with pytest.raises(Exception):
        TradeRequest(outcome_id=1, shares=Decimal("1"), max_slippage_bps=10001)


def test_trade_request_rejects_nonpositive_max_cost():
    """max_cost / min_proceeds 必须 > 0。"""
    with pytest.raises(Exception):
        TradeRequest(outcome_id=1, shares=Decimal("1"), max_cost=Decimal("0"))
    with pytest.raises(Exception):
        TradeRequest(outcome_id=1, shares=Decimal("1"), min_proceeds=Decimal("-1"))


# ────────────────────────────────────────────────
# BUY 滑点
# ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_buy_rejects_above_max_cost(client):
    """max_cost 比实际 pay 低 → 400。"""
    _, h = await _make_user(cash=Decimal("10000"))
    _, outcome_ids = await _make_market(liquidity_b=100.0)  # 大流动性，pay 接近线性
    # 买 1 份，pay 约 0.5；设 max_cost=0.1 → 必拒
    r = await client.post(
        "/api/v1/market/buy",
        json={"outcome_id": outcome_ids[0], "shares": 1, "max_cost": "0.1"},
        headers=h,
    )
    assert r.status_code == 400, r.text
    assert "max_cost" in r.text or "滑点" in r.text


@pytest.mark.asyncio
async def test_buy_rejects_above_hardcap_slippage(client):
    """max_slippage_bps 给得过大也会被服务端 hardcap=1000 截断。"""
    _, h = await _make_user(cash=Decimal("10000"))
    # b=10、双结果、买 5 份，LMSR 滑点 ~12.6% → 超 10% hardcap。
    _, outcome_ids = await _make_market(liquidity_b=10.0, n_outcomes=2)
    r = await client.post(
        "/api/v1/market/buy",
        json={
            "outcome_id": outcome_ids[0],
            "shares": 5,
            "max_slippage_bps": 9999,  # 客户端给 99.99%，会被截到 10%
        },
        headers=h,
    )
    assert r.status_code == 400, r.text
    assert "滑点" in r.text


@pytest.mark.asyncio
async def test_buy_uses_default_slippage_when_unset(client):
    """客户端不传 max_slippage_bps → 服务端用 DEFAULT_SLIPPAGE_BPS=500（5%）兜底。"""
    _, h = await _make_user(cash=Decimal("10000"))
    # b=10、双结果、买 2 份，滑点 ~5%（恰边界，4.99 左右），buy 3 份必然超 5%。
    _, outcome_ids = await _make_market(liquidity_b=10.0, n_outcomes=2)
    r = await client.post(
        "/api/v1/market/buy",
        json={"outcome_id": outcome_ids[0], "shares": 3},  # 不传 bps
        headers=h,
    )
    assert r.status_code == 400, r.text
    assert "滑点" in r.text


@pytest.mark.asyncio
async def test_buy_accepts_when_within_slippage(client):
    """大流动性 + 小成交 + 默认 bps，应正常成交。"""
    _, h = await _make_user(cash=Decimal("10000"))
    _, outcome_ids = await _make_market(liquidity_b=10000.0, n_outcomes=2)
    r = await client.post(
        "/api/v1/market/buy",
        json={"outcome_id": outcome_ids[0], "shares": 1},
        headers=h,
    )
    assert r.status_code == 200, r.text


# ────────────────────────────────────────────────
# SELL 滑点
# ────────────────────────────────────────────────

async def _seed_position(client, headers, outcome_id, shares=1):
    """先 buy 来塞个仓位（大流动性确保 buy 通过）。"""
    r = await client.post(
        "/api/v1/market/buy",
        json={"outcome_id": outcome_id, "shares": shares},
        headers=headers,
    )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_sell_rejects_below_min_proceeds(client):
    """min_proceeds 高于实际 net → 400。"""
    _, h = await _make_user(cash=Decimal("100000"))
    _, outcome_ids = await _make_market(liquidity_b=10000.0, n_outcomes=2)
    await _seed_position(client, h, outcome_ids[0], shares=10)
    # 卖 1 份 net 约 0.5；min_proceeds=10000 必拒
    r = await client.post(
        "/api/v1/market/sell",
        json={"outcome_id": outcome_ids[0], "shares": 1, "min_proceeds": "10000"},
        headers=h,
    )
    assert r.status_code == 400, r.text
    assert "min_proceeds" in r.text or "滑点" in r.text


async def _inject_position(user_id: int, outcome_id: int, shares: Decimal, total_shares: Decimal):
    """直接 DB 注入仓位+市场总份额，绕过 buy 的滑点保护（用于测 sell）。"""
    from app.models.base import Position
    from sqlmodel import select as sm_select
    async with async_session_maker() as s:
        async with s.begin():
            s.add(Position(
                user_id=user_id,
                outcome_id=outcome_id,
                amount=shares,
                cost_basis=Decimal("0"),
            ))
            o_res = await s.execute(sm_select(Outcome).where(Outcome.id == outcome_id))
            o = o_res.scalars().first()
            o.total_shares = total_shares
            s.add(o)


@pytest.mark.asyncio
async def test_sell_uses_default_slippage(client):
    """
    SELL 不传 bps → 默认 5%。
    场景：b=10，市场总份额 q=[5,0]，用户卖 3 份。
    边际价 e^0.5/(e^0.5+1) ≈ 0.622，期望 proceeds = 0.622*3 = 1.866
    实际 proceeds = 10*ln(e^0.5+1) - 10*ln(e^0.2+1) ≈ 1.76
    比例 1.76 / 1.866 ≈ 0.943 → ~5.7% 滑点 > 默认 5% → 应 400
    """
    uid, h = await _make_user(cash=Decimal("100000"))
    _, outcome_ids = await _make_market(liquidity_b=10.0, n_outcomes=2)
    await _inject_position(uid, outcome_ids[0], shares=Decimal("5"), total_shares=Decimal("5"))

    r = await client.post(
        "/api/v1/market/sell",
        json={"outcome_id": outcome_ids[0], "shares": 3},
        headers=h,
    )
    assert r.status_code == 400, r.text
    assert "滑点" in r.text


@pytest.mark.asyncio
async def test_sell_accepts_when_within_slippage(client):
    """大流动性 + 小成交 → 正常卖出。"""
    _, h = await _make_user(cash=Decimal("100000"))
    _, outcome_ids = await _make_market(liquidity_b=10000.0, n_outcomes=2)
    await _seed_position(client, h, outcome_ids[0], shares=1)
    r = await client.post(
        "/api/v1/market/sell",
        json={"outcome_id": outcome_ids[0], "shares": 1},
        headers=h,
    )
    assert r.status_code == 200, r.text


# ────────────────────────────────────────────────
# 锁顺序回归（源码级 assert）
# ────────────────────────────────────────────────

def test_lock_order_smoke():
    """
    读源码确认 SELL handler 中 _lock_market 出现在 select(Position) 之前。
    防止未来 PR 静默回退锁顺序触发死锁。

    P1 follow-up 后：_lock_outcome 从 buy/sell 热路径移除，
    改为先无锁读 market_id，再按 market → all_outcomes → user → position 统一加锁。
    """
    src = Path(__file__).resolve().parent.parent / "app" / "api" / "v1" / "market.py"
    text = src.read_text(encoding="utf-8")

    m = re.search(r"async def sell_shares\b", text)
    assert m, "找不到 sell_shares 函数定义"
    sell_start = m.start()
    next_fn = re.search(r"\n(@router|async def )", text[sell_start + 1:])
    sell_end = sell_start + 1 + next_fn.start() if next_fn else len(text)
    sell_body = text[sell_start:sell_end]

    lock_market_pos = sell_body.find("_lock_market(")
    select_position_pos = sell_body.find("select(Position)")
    assert lock_market_pos != -1, "sell 函数中缺失 _lock_market 调用"
    assert select_position_pos != -1, "sell 函数中缺失 select(Position) 调用"
    assert lock_market_pos < select_position_pos, (
        "SELL 锁顺序回退：_lock_market 必须在 select(Position) 之前出现"
    )
