"""buy/sell 响应 new_cash 是 6dp 全精度（客户端本地 apply 的 cash 基线，spec §6.4）。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from decimal import Decimal

import pytest

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import Market, MarketStatus, Outcome, User


async def _make_user(cash=Decimal("1000")):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=f"u_{suffix}", email=f"{suffix}@t.com",
                     casdoor_id=f"cd_{suffix}", cash=cash, debt=Decimal("0"))
            s.add(u)
            await s.flush()
            uid = u.id
    token = create_access_token(uid)
    return uid, {"Authorization": f"Bearer {token}"}


async def _seed_market():
    async with async_session_maker() as s:
        m = Market(title="t", description="", liquidity_b=100.0, status=MarketStatus.TRADING)
        s.add(m)
        await s.flush()
        oids = []
        for label in ("a", "b"):
            o = Outcome(market_id=m.id, label=label, total_shares=Decimal("0"))
            s.add(o)
            await s.flush()
            oids.append(o.id)
        await s.commit()
        return m.id, oids


@pytest.mark.asyncio
async def test_buy_then_sell_new_cash_is_6dp_exact(client):
    """new_cash 与 DB 里 user.cash 的 6dp 值完全一致（非 2dp 舍入）。"""
    uid, h = await _make_user(cash=Decimal("1000"))
    _, oids = await _seed_market()

    r = await client.post("/api/v1/market/buy", headers=h,
                          json={"outcome_id": oids[0], "shares": 7,
                                "accept_any_slippage": True})
    assert r.status_code == 200, r.text
    new_cash_resp = Decimal(str(r.json()["new_cash"]))
    async with async_session_maker() as s:
        u = await s.get(User, uid)
        assert new_cash_resp == u.cash.quantize(Decimal("0.000001"))
        # 7 份对 b=100 的成本带 6dp 尾数——若响应被 2dp 截断这里必炸
        assert u.cash.quantize(Decimal("0.000001")) != u.cash.quantize(Decimal("0.01"))

    r2 = await client.post("/api/v1/market/sell", headers=h,
                           json={"outcome_id": oids[0], "shares": 3,
                                 "accept_any_slippage": True})
    assert r2.status_code == 200, r2.text
    new_cash_resp2 = Decimal(str(r2.json()["new_cash"]))
    async with async_session_maker() as s:
        u = await s.get(User, uid)
        assert new_cash_resp2 == u.cash.quantize(Decimal("0.000001"))
