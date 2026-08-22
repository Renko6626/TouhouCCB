"""「大赦天下」批量重置：清债（先结息）+ 现金还原到目标值；持仓不动。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import User, SiteConfig
from app.models.ledger import LedgerEntry

URL = "/api/v1/admin/users/batch/amnesty"


@pytest_asyncio.fixture(autouse=True)
async def _seed_cfg(setup_db):
    async with async_session_maker() as s:
        async with s.begin():
            s.add(SiteConfig(key="loan_enabled", value="true", value_type="bool"))
            s.add(SiteConfig(key="loan_daily_rate", value="0.10", value_type="decimal"))
            s.add(SiteConfig(key="initial_balance", value="100", value_type="decimal"))


async def _seed(cash="100", debt="0", superuser=False, accrued_days_ago=None) -> int:
    sfx = uuid.uuid4().hex[:8]
    async with async_session_maker() as s:
        u = User(username=f"u_{sfx}", email=f"{sfx}@t.com", casdoor_id=f"cd_{sfx}",
                 cash=Decimal(cash), debt=Decimal(debt), is_superuser=superuser)
        if accrued_days_ago is not None:
            u.debt_last_accrued_at = datetime.now(timezone.utc) - timedelta(days=accrued_days_ago)
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return u.id


@pytest_asyncio.fixture
async def admin_headers(client):
    uid = await _seed(cash="0", superuser=True)
    return {"Authorization": f"Bearer {create_access_token(uid)}"}


async def _get(uid: int) -> User:
    async with async_session_maker() as s:
        return (await s.execute(select(User).where(User.id == uid))).scalar_one()


async def _ledger(uid: int):
    async with async_session_maker() as s:
        return (await s.execute(select(LedgerEntry).where(LedgerEntry.user_id == uid))).scalars().all()


def _req(**kw):
    base = {"filter": {}, "reason": "大赦", "dry_run": True}
    base.update(kw)
    return base


@pytest.mark.asyncio
async def test_requires_superuser(client):
    uid = await _seed()
    h = {"Authorization": f"Bearer {create_access_token(uid)}"}
    r = await client.post(URL, headers=h, json=_req())
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_dry_run_previews_without_writing(client, admin_headers):
    rich = await _seed(cash="500", debt="0")
    poor = await _seed(cash="3", debt="80")
    r = await client.post(URL, headers=admin_headers, json=_req(dry_run=True))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["reset_cash_to"] == 100.0
    assert body["matched_count"] == 2
    by_id = {m["id"]: m for m in body["matched_users"]}
    assert by_id[rich]["cash_after"] == 100.0 and by_id[poor]["debt_after"] == 0.0
    assert body["total_cash_delta"] == pytest.approx(-400 + 97)
    assert body["total_debt_forgiven"] == 80.0
    # 未落库
    assert (await _get(rich)).cash == Decimal("500")
    assert (await _get(poor)).debt == Decimal("80")
    assert await _ledger(poor) == []


@pytest.mark.asyncio
async def test_execute_resets_cash_and_clears_debt_with_single_ledger_entry(client, admin_headers):
    rich = await _seed(cash="500", debt="0")
    poor = await _seed(cash="3", debt="80")
    r = await client.post(URL, headers=admin_headers, json=_req(dry_run=False))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["updated_count"] == 2
    assert body["total_debt_forgiven"] == 80.0

    u_rich, u_poor = await _get(rich), await _get(poor)
    assert u_rich.cash == Decimal("100") and u_rich.debt == 0
    assert u_poor.cash == Decimal("100") and u_poor.debt == 0
    assert u_poor.debt_last_accrued_at is None

    e_rich, e_poor = await _ledger(rich), await _ledger(poor)
    assert len(e_rich) == 1 and len(e_poor) == 1
    assert e_rich[0].entry_type == "admin_amnesty"
    assert e_rich[0].cash_delta == Decimal("-400") and e_rich[0].debt_delta == 0
    assert e_poor[0].cash_delta == Decimal("97") and e_poor[0].debt_delta == Decimal("-80")
    assert e_poor[0].cash_after == Decimal("100") and e_poor[0].debt_after == 0
    assert e_poor[0].reason == "大赦"


@pytest.mark.asyncio
async def test_accrues_interest_before_forgiving(client, admin_headers):
    # 10%/日，欠 100 已 1 天未结 → 结息后 ~110，全部免掉
    uid = await _seed(cash="0", debt="100", accrued_days_ago=1)
    r = await client.post(URL, headers=admin_headers, json=_req(dry_run=False))
    assert r.status_code == 200, r.text
    row = next(x for x in r.json()["updated"] if x["user_id"] == uid)
    assert row["debt_forgiven"] == pytest.approx(110.0, abs=0.05)
    assert (await _get(uid)).debt == 0
    e = (await _ledger(uid))[0]
    assert e.debt_delta == pytest.approx(Decimal("-110"), abs=Decimal("0.05"))
    assert e.daily_rate_at_event == Decimal("0.10")


@pytest.mark.asyncio
async def test_forgive_debt_false_only_resets_cash(client, admin_headers):
    uid = await _seed(cash="7", debt="50")
    r = await client.post(URL, headers=admin_headers, json=_req(dry_run=False, forgive_debt=False))
    assert r.status_code == 200, r.text
    u = await _get(uid)
    assert u.cash == Decimal("100") and u.debt == Decimal("50")
    e = (await _ledger(uid))[0]
    assert e.debt_delta == 0 and e.cash_delta == Decimal("93")


@pytest.mark.asyncio
async def test_custom_reset_cash_to_and_superuser_excluded_by_default(client, admin_headers):
    normal = await _seed(cash="1")
    boss = await _seed(cash="1", superuser=True)
    r = await client.post(URL, headers=admin_headers, json=_req(dry_run=False, reset_cash_to="250"))
    assert r.status_code == 200, r.text
    ids = {x["user_id"] for x in r.json()["updated"]}
    assert normal in ids and boss not in ids
    assert (await _get(normal)).cash == Decimal("250")
    assert (await _get(boss)).cash == Decimal("1")


@pytest.mark.asyncio
async def test_validation(client, admin_headers):
    r = await client.post(URL, headers=admin_headers, json=_req(reason=""))
    assert r.status_code == 422
    r = await client.post(URL, headers=admin_headers, json=_req(reset_cash_to="-1"))
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_hardcap(client, admin_headers, monkeypatch):
    from app.services import admin_user_service
    monkeypatch.setattr(admin_user_service, "BATCH_HARDCAP", 1)
    await _seed(); await _seed()
    r = await client.post(URL, headers=admin_headers, json=_req())
    assert r.status_code == 400 and "上限" in r.json()["detail"]


@pytest.mark.asyncio
async def test_large_debt_clears_exactly_no_dust(client, admin_headers, monkeypatch):
    """审计 M2：显式结息与 decrease_debt_locked 内部结息必须用同一个 now。
    模拟两次 _compat_now 相差 50ms：大额债务下第二次结息会出 6dp 非零增量，
    修复前留下灰尘债且 debt_last_accrued_at 不清空。"""
    from app.services import loan_service
    real_now = loan_service._compat_now
    calls = {"n": 0}

    def drifting_now(u):
        calls["n"] += 1
        return real_now(u) + timedelta(milliseconds=50 * calls["n"])
    monkeypatch.setattr(loan_service, "_compat_now", drifting_now)

    uid = await _seed(cash="0", debt="1000000", accrued_days_ago=1)
    r = await client.post(URL, headers=admin_headers, json=_req(dry_run=False))
    assert r.status_code == 200, r.text
    u = await _get(uid)
    assert u.debt == 0 and u.debt_last_accrued_at is None
    e = (await _ledger(uid))[0]
    assert e.debt_after == 0 and e.debt_last_accrued_at_after is None
    assert e.debt_delta <= Decimal("-1000000")          # 本金 + 结息全部免除
