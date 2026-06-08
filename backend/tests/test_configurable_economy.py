"""可配置经济参数：sell_fee_rate / initial_balance（admin 热配）。

覆盖：
- site_config.get_decimal_or 安全读取（缺失回落默认）
- 两个新 key 进 whitelist + 默认 seed + 区间校验
- LCV 估值按配置扣卖出手续费；key 缺失回落 0（现有行为不变）
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.core.database import async_session_maker
from app.models.base import Market, MarketStatus, Outcome, Position, SiteConfig, User


# ── 测试种子助手（沿用 test_wealth_mtm 模式）──

async def _seed_user(*, cash: Decimal = Decimal("100")) -> int:
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as s:
        u = User(
            username=f"econ_{suffix}", email=f"{suffix}@t.com",
            casdoor_id=f"cd_{suffix}", cash=cash,
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return u.id


async def _seed_position(*, user_id: int, amount: Decimal, b: float = 100.0) -> None:
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        m = Market(title=f"m_{suffix}", description="econ", liquidity_b=b,
                   status=MarketStatus.TRADING, tags="")
        s.add(m)
        await s.flush()
        o0 = Outcome(market_id=m.id, label="A", total_shares=amount)
        o1 = Outcome(market_id=m.id, label="B", total_shares=Decimal("0"))
        s.add_all([o0, o1])
        await s.flush()
        s.add(Position(user_id=user_id, outcome_id=o0.id, amount=amount,
                       cost_basis=Decimal("0")))
        await s.commit()


async def _set_config(key: str, value: str, value_type: str = "decimal") -> None:
    async with async_session_maker() as s:
        async with s.begin():
            s.add(SiteConfig(key=key, value=value, value_type=value_type))


# ── get_decimal_or ──

@pytest.mark.asyncio
async def test_get_decimal_or_returns_value_when_present():
    from app.services.site_config import get_decimal_or
    await _set_config("econ_x", "0.07")
    async with async_session_maker() as s:
        v = await get_decimal_or(s, "econ_x", Decimal("0"))
    assert v == Decimal("0.07")


@pytest.mark.asyncio
async def test_get_decimal_or_returns_default_when_missing():
    from app.services.site_config import get_decimal_or
    async with async_session_maker() as s:
        v = await get_decimal_or(s, "definitely_missing_key", Decimal("3.5"))
    assert v == Decimal("3.5")


# ── whitelist + 默认 seed ──

def test_new_keys_in_whitelist():
    from app.api.v1.site_config import _WHITELIST
    assert _WHITELIST.get("sell_fee_rate") == "decimal"
    assert _WHITELIST.get("initial_balance") == "decimal"


@pytest.mark.asyncio
async def test_defaults_seeded(client):
    from app.services.loan_migrate import auto_migrate
    from app.services.site_config import get_decimal
    await auto_migrate()
    async with async_session_maker() as s:
        assert await get_decimal(s, "sell_fee_rate") == Decimal("0")
        assert await get_decimal(s, "initial_balance") == Decimal("100")


# ── 校验区间 ──

def test_sell_fee_rate_validation():
    from app.api.v1.site_config import _validate
    _validate("sell_fee_rate", "0")        # ok
    _validate("sell_fee_rate", "0.05")     # ok
    with pytest.raises(HTTPException):
        _validate("sell_fee_rate", "0.2")  # 上限不含
    with pytest.raises(HTTPException):
        _validate("sell_fee_rate", "-0.01")


def test_initial_balance_validation():
    from app.api.v1.site_config import _validate
    _validate("initial_balance", "0")          # ok
    _validate("initial_balance", "100")        # ok
    _validate("initial_balance", "1000000")    # ok 上限
    with pytest.raises(HTTPException):
        _validate("initial_balance", "-1")
    with pytest.raises(HTTPException):
        _validate("initial_balance", "1000001")


# ── LCV 估值按配置扣卖出手续费 ──

@pytest.mark.asyncio
async def test_lcv_deducts_configured_sell_fee():
    """设 sell_fee_rate=0.1 后，LCV(sentinel) ≈ gross × 0.9。"""
    from app.services.wealth import compute_users_holdings_value
    await _set_config("sell_fee_rate", "0.1")
    uid = await _seed_user()
    await _seed_position(user_id=uid, amount=Decimal("200"))

    async with async_session_maker() as s:
        lcv = await compute_users_holdings_value(s, user_ids=[uid])
        gross = await compute_users_holdings_value(
            s, user_ids=[uid], sell_fee_rate=Decimal("0"))

    assert gross[uid] > Decimal("0")
    ratio = lcv[uid] / gross[uid]
    assert abs(ratio - Decimal("0.9")) < Decimal("0.001"), (
        f"配置 10% 卖出费应让 LCV≈gross×0.9，实得 ratio={ratio}")


@pytest.mark.asyncio
async def test_lcv_falls_back_to_zero_fee_when_key_missing():
    """sell_fee_rate 未 seed 时回落 0（保持现有行为，不抛）。"""
    from app.services.wealth import compute_users_holdings_value
    uid = await _seed_user()
    await _seed_position(user_id=uid, amount=Decimal("200"))

    async with async_session_maker() as s:
        lcv = await compute_users_holdings_value(s, user_ids=[uid])
        gross = await compute_users_holdings_value(
            s, user_ids=[uid], sell_fee_rate=Decimal("0"))

    assert lcv[uid] == gross[uid], "key 缺失应等价于 fee=0"


# ── market 卖出 hot path 按配置扣费 ──

async def _make_user_token(cash: Decimal = Decimal("100000")):
    from app.core.users import create_access_token
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as s:
        u = User(username=f"econ_{suffix}", email=f"{suffix}@t.com",
                 casdoor_id=f"cd_{suffix}", cash=cash, debt=Decimal("0"))
        s.add(u)
        await s.commit()
        await s.refresh(u)
        uid = u.id
    return uid, {"Authorization": f"Bearer {create_access_token(uid)}"}


async def _make_market_outcome(b: float = 10000.0):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        m = Market(title=f"m_{suffix}", description="econ", liquidity_b=b,
                   status=MarketStatus.TRADING, tags="")
        s.add(m)
        await s.flush()
        o0 = Outcome(market_id=m.id, label="A", total_shares=Decimal("0"))
        o1 = Outcome(market_id=m.id, label="B", total_shares=Decimal("0"))
        s.add_all([o0, o1])
        await s.commit()
        await s.refresh(o0)
        return m.id, o0.id


@pytest.mark.asyncio
async def test_quote_sell_applies_configured_fee(client):
    await _set_config("sell_fee_rate", "0.1")
    _, h = await _make_user_token()
    _, oid = await _make_market_outcome()
    rb = await client.post("/api/v1/market/buy", headers=h,
                           json={"outcome_id": oid, "shares": "100"})
    assert rb.status_code == 200, rb.text
    rq = await client.post("/api/v1/market/quote", headers=h,
                           json={"outcome_id": oid, "shares": "50", "side": "sell"})
    assert rq.status_code == 200, rq.text
    d = rq.json()
    gross, fee, net = Decimal(str(d["gross"])), Decimal(str(d["fee"])), Decimal(str(d["net"]))
    assert gross > 0
    assert abs(fee / gross - Decimal("0.1")) < Decimal("0.002"), f"fee/gross={fee/gross}"
    assert abs(net - (gross - fee)) < Decimal("0.00001")


@pytest.mark.asyncio
async def test_sell_execution_records_configured_fee(client):
    from sqlalchemy import select, desc
    from app.models.base import Transaction
    await _set_config("sell_fee_rate", "0.1")
    uid, h = await _make_user_token()
    _, oid = await _make_market_outcome()
    await client.post("/api/v1/market/buy", headers=h,
                      json={"outcome_id": oid, "shares": "100"})
    rs = await client.post("/api/v1/market/sell", headers=h,
                           json={"outcome_id": oid, "shares": "50"})
    assert rs.status_code == 200, rs.text
    async with async_session_maker() as s:
        tx = (await s.execute(
            select(Transaction).where(Transaction.user_id == uid)
            .order_by(desc(Transaction.id))
        )).scalars().first()
    assert tx is not None
    assert tx.fee > Decimal("0"), "配置了 10% 卖出费，sell 交易应记录非零 fee"
