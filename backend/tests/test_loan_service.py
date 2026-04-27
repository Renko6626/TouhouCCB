import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest, pytest_asyncio, uuid
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from sqlmodel import SQLModel
from app.core.database import engine, async_session_maker
from app.models.base import User
from app.services.loan_service import accrue_interest


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
        await conn.run_sync(SQLModel.metadata.create_all)


def _new_user_sync(cash=Decimal("100"), debt=Decimal("0"), last_accrued=None):
    return User(
        username=f"u_{uuid.uuid4().hex[:6]}",
        email=f"{uuid.uuid4().hex[:6]}@t.com",
        casdoor_id=uuid.uuid4().hex,
        cash=cash,
        debt=debt,
        debt_last_accrued_at=last_accrued,
    )


def test_accrue_noop_when_debt_zero():
    u = _new_user_sync(debt=Decimal("0"), last_accrued=datetime.now(timezone.utc) - timedelta(hours=1))
    before = u.debt
    accrue_interest(u, Decimal("0.01"), datetime.now(timezone.utc))
    assert u.debt == before


def test_accrue_noop_when_last_accrued_none():
    u = _new_user_sync(debt=Decimal("100"), last_accrued=None)
    before = u.debt
    accrue_interest(u, Decimal("0.01"), datetime.now(timezone.utc))
    assert u.debt == before


def test_accrue_one_full_day_at_1pct():
    now = datetime.now(timezone.utc)
    u = _new_user_sync(debt=Decimal("1000"), last_accrued=now - timedelta(days=1))
    accrue_interest(u, Decimal("0.01"), now)
    # 线性近似一天：1000 * (1 + 0.01) = 1010
    assert abs(u.debt - Decimal("1010")) < Decimal("0.01")
    assert u.debt_last_accrued_at == now


def test_accrue_compounds_across_sweeps():
    """10 次连续 sweep，每次 60s，每次作用在当前 debt 上（复利）。"""
    now = datetime.now(timezone.utc)
    u = _new_user_sync(debt=Decimal("1000"), last_accrued=now)
    rate = Decimal("0.01")
    for i in range(10):
        now = now + timedelta(seconds=60)
        accrue_interest(u, rate, now)
    expected = Decimal("1000") * (Decimal(1) + rate * Decimal(60) / Decimal(86400)) ** 10
    assert abs(u.debt - expected) < Decimal("0.0001")


def test_accrue_negative_elapsed_noop():
    """时钟倒退不应倒扣利息（安全兜底）。"""
    now = datetime.now(timezone.utc)
    u = _new_user_sync(debt=Decimal("1000"), last_accrued=now + timedelta(hours=1))
    before = u.debt
    accrue_interest(u, Decimal("0.01"), now)
    assert u.debt == before


from app.services.loan_service import increase_debt, decrease_debt, compute_max_borrow


async def _create_user_in_db(**kwargs) -> int:
    async with async_session_maker() as s:
        async with s.begin():
            u = _new_user_sync(**kwargs)
            s.add(u)
            await s.flush()
            return u.id


@pytest.mark.asyncio
async def test_increase_debt_grant_cash_true():
    uid = await _create_user_in_db(cash=Decimal("100"), debt=Decimal("0"))
    async with async_session_maker() as s:
        u = await increase_debt(s, uid, Decimal("50"), grant_cash=True, daily_rate=Decimal("0.01"))
        await s.commit()
    async with async_session_maker() as s:
        u2 = await s.get(User, uid)
    assert u2.cash == Decimal("150.000000")
    assert u2.debt == Decimal("50.000000")
    assert u2.debt_last_accrued_at is not None


@pytest.mark.asyncio
async def test_increase_debt_accrues_existing_debt():
    """已有债务时先 accrue 再加 amount。"""
    now = datetime.now(timezone.utc)
    uid = await _create_user_in_db(
        cash=Decimal("0"),
        debt=Decimal("1000"),
        last_accrued=now - timedelta(days=1),
    )
    async with async_session_maker() as s:
        await increase_debt(s, uid, Decimal("100"), grant_cash=True, daily_rate=Decimal("0.01"))
        await s.commit()
    async with async_session_maker() as s:
        u = await s.get(User, uid)
    # 1000 * 1.01 + 100 ≈ 1110
    assert abs(u.debt - Decimal("1110")) < Decimal("0.1")
    assert u.cash == Decimal("100.000000")


@pytest.mark.asyncio
async def test_decrease_debt_consume_cash_true_partial():
    uid = await _create_user_in_db(cash=Decimal("200"), debt=Decimal("100"), last_accrued=datetime.now(timezone.utc))
    async with async_session_maker() as s:
        u, eff = await decrease_debt(s, uid, Decimal("30"), consume_cash=True, daily_rate=Decimal("0.01"))
        await s.commit()
    assert eff == Decimal("30") or eff == Decimal("30.000000")
    async with async_session_maker() as s:
        u2 = await s.get(User, uid)
    # cash 不受 accrue 影响，严格等；debt 可能因微秒级 accrue 略大于 70
    assert u2.cash == Decimal("170.000000")
    assert abs(u2.debt - Decimal("70")) < Decimal("0.001")
    assert u2.debt_last_accrued_at is not None  # 仍有 debt


@pytest.mark.asyncio
async def test_decrease_debt_overpay_clamps_and_clears_timestamp():
    uid = await _create_user_in_db(cash=Decimal("1000"), debt=Decimal("100"), last_accrued=datetime.now(timezone.utc))
    async with async_session_maker() as s:
        u, eff = await decrease_debt(s, uid, Decimal("9999"), consume_cash=True, daily_rate=Decimal("0.01"))
        await s.commit()
    # effective 约等于 debt（微秒级 accrue 可能让它略大于 100）
    assert abs(eff - Decimal("100")) < Decimal("0.001")
    async with async_session_maker() as s:
        u2 = await s.get(User, uid)
    assert u2.debt == Decimal("0.000000") or u2.debt == Decimal("0")
    # cash = 1000 - effective
    assert abs(u2.cash - Decimal("900")) < Decimal("0.001")
    assert u2.debt_last_accrued_at is None


@pytest.mark.asyncio
async def test_decrease_debt_forgive_no_cash_change():
    uid = await _create_user_in_db(cash=Decimal("200"), debt=Decimal("100"), last_accrued=datetime.now(timezone.utc))
    async with async_session_maker() as s:
        u, eff = await decrease_debt(s, uid, Decimal("40"), consume_cash=False, daily_rate=Decimal("0.01"))
        await s.commit()
    async with async_session_maker() as s:
        u2 = await s.get(User, uid)
    assert u2.cash == Decimal("200.000000")
    # 微秒级 accrue 可能让 debt 略大于 60
    assert abs(u2.debt - Decimal("60")) < Decimal("0.001")


def test_compute_max_borrow_basic():
    u = _new_user_sync(cash=Decimal("100"), debt=Decimal("0"))
    # net_worth = 100 + 50 = 150, k=1 → max=150
    assert compute_max_borrow(u, holdings_value=Decimal("50"), k=Decimal("1")) == Decimal("150")


def test_compute_max_borrow_with_existing_debt_returns_zero():
    u = _new_user_sync(cash=Decimal("100"), debt=Decimal("80"))
    # net_worth = 100 - 80 + 0 = 20, k=2 → k*nw=40, max=40-80=<0→0
    assert compute_max_borrow(u, holdings_value=Decimal("0"), k=Decimal("2")) == Decimal("0")


def test_compute_max_borrow_positive_headroom():
    u = _new_user_sync(cash=Decimal("500"), debt=Decimal("100"))
    # net_worth = 500-100+200 = 600, k=1 → 600-100=500
    assert compute_max_borrow(u, holdings_value=Decimal("200"), k=Decimal("1")) == Decimal("500")


# ===== 复利场景下 cash 不跑负的回归测试（2026-04-27 用户报告） =====

@pytest.mark.asyncio
async def test_decrease_debt_consume_cash_capped_by_cash_under_compounding():
    """场景：cash 刚好等于 pre-accrual debt，但累计利息让 post-accrual debt > cash。
    服务层应取 min(amount, post-accrual debt, cash) 防止 cash 跑负。"""
    one_day_ago = datetime.now(timezone.utc) - timedelta(days=1)
    # cash=1000 = pre-accrual debt；24h @ 1%/day → post-accrual debt ≈ 1010
    uid = await _create_user_in_db(
        cash=Decimal("1000"), debt=Decimal("1000"), last_accrued=one_day_ago,
    )
    async with async_session_maker() as s:
        u, eff = await decrease_debt(
            s, uid, Decimal("3000"), consume_cash=True, daily_rate=Decimal("0.01"),
        )
        await s.commit()
    async with async_session_maker() as s:
        u2 = await s.get(User, uid)
    # cash 必须 >= 0
    assert u2.cash >= Decimal("0"), f"cash 跑负了: {u2.cash}"
    # effective 不应超过初始 cash
    assert eff <= Decimal("1000.000001"), f"effective 超过 cash: {eff}"
    # 剩余 debt 是真实利息部分（post-accrual debt - effective）
    assert u2.debt >= Decimal("0")


@pytest.mark.asyncio
async def test_decrease_debt_amount_exceeds_debt_caps_at_debt():
    """用户输入 3000 还款，但只欠 1000：cash 只扣 1000，不会扣 3000。"""
    uid = await _create_user_in_db(cash=Decimal("5000"), debt=Decimal("1000"), last_accrued=datetime.now(timezone.utc))
    async with async_session_maker() as s:
        u, eff = await decrease_debt(s, uid, Decimal("3000"), consume_cash=True, daily_rate=Decimal("0.01"))
        await s.commit()
    async with async_session_maker() as s:
        u2 = await s.get(User, uid)
    # cash 只扣了 ~1000（不是 3000）
    assert abs(u2.cash - Decimal("4000")) < Decimal("0.01"), f"cash 应约 4000, 实为 {u2.cash}"
    assert u2.debt == Decimal("0")
    assert abs(eff - Decimal("1000")) < Decimal("0.01")
