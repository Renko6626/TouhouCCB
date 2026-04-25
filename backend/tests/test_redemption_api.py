import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from asgi_lifespan import LifespanManager
from sqlmodel import SQLModel

from app.main import app
from app.core.database import engine, async_session_maker
from app.core.users import create_access_token
from app.models.base import User
from app.models.redemption import (
    RedemptionPartner, RedemptionBatch, RedemptionCode,
    BatchStatus, CodeStatus,
)


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
        await conn.run_sync(SQLModel.metadata.create_all)
    yield


@pytest_asyncio.fixture
async def client():
    async with LifespanManager(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac


async def _make_user(superuser=False, cash=Decimal("100")):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        u = User(
            username=f"u_{suffix}",
            email=f"{suffix}@t.com",
            casdoor_id=f"cd_{suffix}",
            cash=cash, is_superuser=superuser,
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        token = create_access_token(u.id)
        return u.id, {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_admin_full_flow_and_user_buys(client):
    """端到端：建合作方 → 建批次 → CSV 导入 → 上架 → 用户购买 → 我的记录 → 标记已用。"""
    _, admin_h = await _make_user(superuser=True)
    buyer_id, buyer_h = await _make_user(cash=Decimal("100"))

    # 1. 创建合作方
    r = await client.post(
        "/api/v1/admin/redemption/partners",
        json={"name": "朋友A", "description": "", "website_url": "https://a.example", "is_active": True},
        headers=admin_h,
    )
    assert r.status_code == 200, r.text
    partner_id = r.json()["id"]

    # 2. 创建批次（默认 draft）
    r = await client.post(
        "/api/v1/admin/redemption/batches",
        json={"partner_id": partner_id, "name": "5元券",
              "description": "## 说明\n去 a.example 兑换", "unit_price": "30"},
        headers=admin_h,
    )
    assert r.status_code == 200
    batch_id = r.json()["id"]

    # 3. CSV 预检
    r = await client.post(
        f"/api/v1/admin/redemption/batches/{batch_id}/import/preview",
        json={"csv_text": "code\nABC\nDEF\nABC\n"},
        headers=admin_h,
    )
    assert r.status_code == 200
    p = r.json()
    assert p["new_codes"] == ["ABC", "DEF"]

    # 4. CSV 提交
    r = await client.post(
        f"/api/v1/admin/redemption/batches/{batch_id}/import/commit",
        json={"csv_text": "ABC\nDEF\n", "confirm": True},
        headers=admin_h,
    )
    assert r.status_code == 200
    assert r.json()["inserted"] == 2

    # 5. 上架批次
    r = await client.patch(
        f"/api/v1/admin/redemption/batches/{batch_id}",
        json={"status": "active"},
        headers=admin_h,
    )
    assert r.status_code == 200

    # 6. 用户列表能看到
    r = await client.get("/api/v1/redemption/batches", headers=buyer_h)
    assert r.status_code == 200
    assert any(item["id"] == batch_id for item in r.json())

    # 7. 用户购买
    r = await client.post("/api/v1/redemption/purchase",
                          json={"batch_id": batch_id}, headers=buyer_h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["code_string"] in {"ABC", "DEF"}
    assert Decimal(body["cash_after"]) == Decimal("70")

    # 8. 我的记录
    r = await client.get("/api/v1/redemption/my", headers=buyer_h)
    assert r.status_code == 200
    assert len(r.json()) == 1

    # 9. 标记已用
    code_id = body["code_id"]
    r = await client.post(f"/api/v1/redemption/my/{code_id}/mark-used",
                          json={"used": True}, headers=buyer_h)
    assert r.status_code == 200
    assert r.json()["marked_used_by_user_at"] is not None


@pytest.mark.asyncio
async def test_purchase_insufficient_cash(client):
    _, _ = await _make_user(superuser=True)
    _, poor_h = await _make_user(cash=Decimal("5"))

    async with async_session_maker() as s:
        p = RedemptionPartner(name="P", is_active=True)
        s.add(p); await s.commit(); await s.refresh(p)
        b = RedemptionBatch(partner_id=p.id, name="B", unit_price=Decimal("100"),
                             status=BatchStatus.ACTIVE)
        s.add(b); await s.commit(); await s.refresh(b)
        s.add(RedemptionCode(batch_id=b.id, code_string="X"))
        await s.commit()
        bid = b.id

    r = await client.post("/api/v1/redemption/purchase",
                          json={"batch_id": bid}, headers=poor_h)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_active_batch_cannot_change_price(client):
    _, admin_h = await _make_user(superuser=True)

    async with async_session_maker() as s:
        p = RedemptionPartner(name="P", is_active=True)
        s.add(p); await s.commit(); await s.refresh(p)
        b = RedemptionBatch(partner_id=p.id, name="B", unit_price=Decimal("10"),
                             status=BatchStatus.ACTIVE)
        s.add(b); await s.commit(); await s.refresh(b)
        bid = b.id

    r = await client.patch(f"/api/v1/admin/redemption/batches/{bid}",
                           json={"unit_price": "20"}, headers=admin_h)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_other_user_cannot_view_my_code(client):
    buyer_id, _ = await _make_user(cash=Decimal("100"))
    _, spy_h = await _make_user(cash=Decimal("100"))

    async with async_session_maker() as s:
        p = RedemptionPartner(name="P", is_active=True)
        s.add(p); await s.commit(); await s.refresh(p)
        b = RedemptionBatch(partner_id=p.id, name="B", unit_price=Decimal("10"),
                             status=BatchStatus.ACTIVE)
        s.add(b); await s.commit(); await s.refresh(b)
        c = RedemptionCode(batch_id=b.id, code_string="SECRET",
                           status=CodeStatus.SOLD, bought_by_user_id=buyer_id)
        s.add(c); await s.commit(); await s.refresh(c)
        cid = c.id

    r = await client.get(f"/api/v1/redemption/my/{cid}", headers=spy_h)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_non_admin_cannot_access_admin_api(client):
    _, normal_h = await _make_user(superuser=False)
    r = await client.get("/api/v1/admin/redemption/partners", headers=normal_h)
    assert r.status_code in (401, 403)
