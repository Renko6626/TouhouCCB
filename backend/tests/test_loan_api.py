import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest, pytest_asyncio, uuid
from decimal import Decimal
from sqlmodel import select
from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import User, SiteConfig


@pytest_asyncio.fixture(autouse=True)
async def _seed_loan_config(setup_db):
    """conftest 的 setup_db 负责清库；此 fixture 仅追加 SiteConfig 种子。"""
    async with async_session_maker() as s:
        async with s.begin():
            s.add(SiteConfig(key="loan_enabled", value="true", value_type="bool"))
            s.add(SiteConfig(key="loan_leverage_k", value="1.0", value_type="decimal"))
            s.add(SiteConfig(key="loan_daily_rate", value="0.01", value_type="decimal"))
            s.add(SiteConfig(key="loan_sweep_interval_sec", value="60", value_type="int"))


async def _make_user(cash=Decimal("1000"), debt=Decimal("0"), superuser=False):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(
                username=f"u_{suffix}",
                email=f"{suffix}@t.com",
                casdoor_id=f"cd_{suffix}",
                cash=cash,
                debt=debt,
                is_superuser=superuser,
            )
            s.add(u)
            await s.flush()
            uid = u.id
    token = create_access_token(uid)
    return uid, {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_quota_returns_fields(client):
    _, h = await _make_user(cash=Decimal("500"))
    r = await client.get("/api/v1/loan/quota", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is True
    assert Decimal(body["cash"]) == Decimal("500")
    assert Decimal(body["debt"]) == Decimal("0")
    assert Decimal(body["max_borrow"]) >= Decimal("500")
    assert Decimal(body["daily_rate"]) == Decimal("0.01")


@pytest.mark.asyncio
async def test_borrow_success_updates_cash_and_debt(client):
    uid, h = await _make_user(cash=Decimal("1000"))
    r = await client.post("/api/v1/loan/borrow", json={"amount": "500"}, headers=h)
    assert r.status_code == 200, r.text
    async with async_session_maker() as s:
        u = await s.get(User, uid)
    assert u.cash == Decimal("1500.000000")
    assert u.debt == Decimal("500.000000")
    assert u.debt_last_accrued_at is not None


@pytest.mark.asyncio
async def test_borrow_exceeds_quota_400(client):
    _, h = await _make_user(cash=Decimal("100"))
    # k=1, net_worth=100, max_borrow=100；借 200 应拒
    r = await client.post("/api/v1/loan/borrow", json={"amount": "200"}, headers=h)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_borrow_when_disabled_403(client):
    async with async_session_maker() as s:
        async with s.begin():
            result = await s.execute(select(SiteConfig).where(SiteConfig.key == "loan_enabled"))
            row = result.scalar_one()
            row.value = "false"
            s.add(row)
    _, h = await _make_user(cash=Decimal("1000"))
    r = await client.post("/api/v1/loan/borrow", json={"amount": "100"}, headers=h)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_borrow_nonpositive_422(client):
    _, h = await _make_user(cash=Decimal("1000"))
    r = await client.post("/api/v1/loan/borrow", json={"amount": "0"}, headers=h)
    assert r.status_code == 422  # pydantic gt=0


from datetime import datetime, timedelta, timezone
from app.services import loan_service
from app.models.ledger import LedgerEntry
from app.models.audit import AuditEvent


async def _set_fixed_interest_clock(uid, monkeypatch):
    now = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
    async with async_session_maker() as s:
        u = await s.get(User, uid)
        u.debt_last_accrued_at = now - timedelta(days=1)
        await s.commit()
    monkeypatch.setattr(loan_service, "_compat_now", lambda u: now.replace(tzinfo=None)
                        if u.debt_last_accrued_at is not None and u.debt_last_accrued_at.tzinfo is None else now)


@pytest.mark.asyncio
async def test_snapshot_repayment_leaves_newly_accrued_interest(client, monkeypatch):
    uid, h = await _make_user(cash=Decimal("500"), debt=Decimal("100"))
    await _set_fixed_interest_clock(uid, monkeypatch)
    quota = (await client.get("/api/v1/loan/quota", headers=h)).json()
    r = await client.post("/api/v1/loan/repay", json={"amount": quota["debt"]}, headers=h)
    assert r.status_code == 200, r.text
    assert Decimal(r.json()["debt"]) == Decimal("1")


@pytest.mark.asyncio
@pytest.mark.parametrize("cash,debt,remaining,effective", [
    ("500", "100", "0", "101"),
    ("30", "100", "71", "30"),
    ("500", "0.000001", "0", "0.000001"),
])
async def test_repay_all_uses_latest_interest_and_cash_cap(client, monkeypatch, cash, debt, remaining, effective):
    uid, h = await _make_user(cash=Decimal(cash), debt=Decimal(debt))
    await _set_fixed_interest_clock(uid, monkeypatch)
    r = await client.post("/api/v1/loan/repay-all", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert Decimal(body["debt"]) == Decimal(remaining)
    assert Decimal(body["effective"]) == Decimal(effective)
    assert Decimal(body["cash"]) == Decimal(cash) - Decimal(effective)
    async with async_session_maker() as s:
        u = await s.get(User, uid)
        assert u.debt == Decimal(remaining)
        assert (u.debt_last_accrued_at is None) == (u.debt == 0)
        entry = (await s.execute(select(LedgerEntry).where(LedgerEntry.user_id == uid))).scalar_one()
        assert entry.cash_delta == entry.debt_delta == -Decimal(effective)
        event = (await s.execute(select(AuditEvent).where(AuditEvent.user_id == uid))).scalar_one()
        assert Decimal(str(event.payload["interest_accrued"])) == Decimal(remaining) + Decimal(effective) - Decimal(debt)


@pytest.mark.asyncio
async def test_repay_all_already_repaid_is_safe_noop(client):
    uid, h = await _make_user(cash=Decimal("500"))
    r = await client.post("/api/v1/loan/repay-all", headers=h)
    assert r.status_code == 200, r.text
    assert Decimal(r.json()["debt"]) == Decimal(r.json()["effective"]) == 0
    assert Decimal(r.json()["cash"]) == Decimal("500")


@pytest.mark.asyncio
async def test_repay_all_without_cash_fails_without_forgiving_debt(client):
    uid, h = await _make_user(cash=Decimal("0"), debt=Decimal("100"))
    r = await client.post("/api/v1/loan/repay-all", headers=h)
    assert r.status_code == 400, r.text
    async with async_session_maker() as s:
        u = await s.get(User, uid)
        assert u.cash == 0 and u.debt == Decimal("100")


@pytest.mark.asyncio
async def test_repay_all_requires_authentication(client):
    r = await client.post("/api/v1/loan/repay-all")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_repay_partial(client):
    uid, h = await _make_user(cash=Decimal("1000"), debt=Decimal("200"))
    async with async_session_maker() as s:
        async with s.begin():
            u = await s.get(User, uid)
            u.debt_last_accrued_at = datetime.now(timezone.utc)
            s.add(u)
    r = await client.post("/api/v1/loan/repay", json={"amount": "50"}, headers=h)
    assert r.status_code == 200, r.text
    async with async_session_maker() as s:
        u2 = await s.get(User, uid)
    assert u2.cash == Decimal("950.000000")
    # 允许微秒级 accrue 微增
    assert abs(u2.debt - Decimal("150")) < Decimal("0.01")


@pytest.mark.asyncio
async def test_repay_overpay_clamps_to_debt(client):
    uid, h = await _make_user(cash=Decimal("1000"), debt=Decimal("50"))
    async with async_session_maker() as s:
        async with s.begin():
            u = await s.get(User, uid)
            u.debt_last_accrued_at = datetime.now(timezone.utc)
            s.add(u)
    r = await client.post("/api/v1/loan/repay", json={"amount": "9999"}, headers=h)
    assert r.status_code == 200
    async with async_session_maker() as s:
        u2 = await s.get(User, uid)
    assert u2.debt == Decimal("0") or u2.debt == Decimal("0.000000")
    # cash = 1000 - effective（effective 约等于 50，允许微增）
    assert abs(u2.cash - Decimal("950")) < Decimal("0.01")
    assert u2.debt_last_accrued_at is None


@pytest.mark.asyncio
async def test_repay_clamps_to_cash(client):
    """cash 不够还款时 clamp 到 cash 上限：不报 400，effective 截断到 cash，debt 扣相应金额。

    设计意图见 app/api/v1/loan.py:118 注释：「用户输入超额（>debt 或 >cash）会被静默封顶，
    实际扣减由 effective 字段返回」。pre-check 只在 cash<=0 时返回 400。
    """
    uid, h = await _make_user(cash=Decimal("30"), debt=Decimal("200"))
    async with async_session_maker() as s:
        async with s.begin():
            u = await s.get(User, uid)
            u.debt_last_accrued_at = datetime.now(timezone.utc)
            s.add(u)
    r = await client.post("/api/v1/loan/repay", json={"amount": "100"}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    # effective ≈ 30（cash 上限；允许微秒级 accrue 让 debt 微增但不影响 cash 这边的截断）
    assert abs(Decimal(body["effective"]) - Decimal("30")) < Decimal("0.01")
    async with async_session_maker() as s:
        u2 = await s.get(User, uid)
    # cash 清零（被 effective=30 吃完）
    assert u2.cash == Decimal("0") or u2.cash == Decimal("0.000000")
    # debt = 200 - 30 = 170（允许复利微增）
    assert abs(u2.debt - Decimal("170")) < Decimal("0.01")
