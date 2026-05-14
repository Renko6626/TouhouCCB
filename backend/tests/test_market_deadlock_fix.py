"""
跨 outcome 死锁修复回归测试（P1 follow-up）

根本原因：buy/sell 都先 _lock_outcome(单个 outcome) 再 _lock_market，
然后又 _lock_outcomes_for_market（锁全部 outcome）。
两个事务分别持有不同 outcome 的锁，都想拿 market 锁，形成环形等待 → PG 40P01。

修复：用无锁 SELECT 拿 market_id，再按 market → all_outcomes 统一顺序加锁。
"""
import re
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import Market, MarketStatus, Outcome, User


async def _make_user(cash=Decimal("100000")):
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


async def _make_market(liquidity_b: float = 10000.0, n_outcomes: int = 2):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            m = Market(
                title=f"m_{suffix}",
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
    return m.id, outcome_ids


# ────────────────────────────────────────────────
# 源码级断言：锁顺序不形成环
# ────────────────────────────────────────────────

def _get_fn_body(text: str, fn_name: str) -> str:
    m = re.search(rf"async def {fn_name}\b", text)
    assert m, f"找不到 {fn_name} 函数定义"
    start = m.start()
    next_fn = re.search(r"\n(@router|async def )", text[start + 1:])
    end = start + 1 + next_fn.start() if next_fn else len(text)
    return text[start:end]


def test_buy_locks_market_before_individual_outcome():
    """
    buy_shares 不应在 _lock_market 之前调用 _lock_outcome。
    若违反，与另一个持有不同 outcome 的事务必然形成 cross-outcome 环形等待。
    """
    src = Path(__file__).resolve().parent.parent / "app" / "api" / "v1" / "market.py"
    body = _get_fn_body(src.read_text(encoding="utf-8"), "buy_shares")

    lock_market_pos = body.find("_lock_market(")
    lock_outcome_pos = body.find("await _lock_outcome(")

    assert lock_market_pos != -1, "buy_shares 中缺失 _lock_market 调用"
    # _lock_outcome 不应出现在 _lock_market 之前
    assert lock_outcome_pos == -1 or lock_market_pos < lock_outcome_pos, (
        f"buy_shares: _lock_outcome (pos={lock_outcome_pos}) 出现在 _lock_market "
        f"(pos={lock_market_pos}) 之前——跨 outcome 并发必然触发 PG 40P01 死锁"
    )


def test_sell_locks_market_before_individual_outcome():
    """
    sell_shares 不应在 _lock_market 之前调用 _lock_outcome。
    """
    src = Path(__file__).resolve().parent.parent / "app" / "api" / "v1" / "market.py"
    body = _get_fn_body(src.read_text(encoding="utf-8"), "sell_shares")

    lock_market_pos = body.find("_lock_market(")
    lock_outcome_pos = body.find("await _lock_outcome(")

    assert lock_market_pos != -1, "sell_shares 中缺失 _lock_market 调用"
    assert lock_outcome_pos == -1 or lock_market_pos < lock_outcome_pos, (
        f"sell_shares: _lock_outcome (pos={lock_outcome_pos}) 出现在 _lock_market "
        f"(pos={lock_market_pos}) 之前——跨 outcome 并发必然触发 PG 40P01 死锁"
    )


# ────────────────────────────────────────────────
# 功能回归：修复后 buy/sell 仍正常工作
# ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_buy_different_outcomes_same_market_both_succeed(client):
    """两个用户分别买同一市场的不同 outcome，两笔都应该 200。"""
    _, h1 = await _make_user()
    _, h2 = await _make_user()
    _, outcome_ids = await _make_market(liquidity_b=10000.0, n_outcomes=2)

    r1 = await client.post(
        "/api/v1/market/buy",
        json={"outcome_id": outcome_ids[0], "shares": 1},
        headers=h1,
    )
    r2 = await client.post(
        "/api/v1/market/buy",
        json={"outcome_id": outcome_ids[1], "shares": 1},
        headers=h2,
    )
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text


@pytest.mark.asyncio
async def test_buy_then_sell_same_outcome_succeeds(client):
    """同一用户先买后卖同一 outcome，应正常成交。"""
    _, h = await _make_user()
    _, outcome_ids = await _make_market(liquidity_b=10000.0, n_outcomes=2)

    r = await client.post(
        "/api/v1/market/buy",
        json={"outcome_id": outcome_ids[0], "shares": 1},
        headers=h,
    )
    assert r.status_code == 200, r.text

    r = await client.post(
        "/api/v1/market/sell",
        json={"outcome_id": outcome_ids[0], "shares": 1},
        headers=h,
    )
    assert r.status_code == 200, r.text
