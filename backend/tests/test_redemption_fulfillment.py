import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import uuid

import pytest
from sqlalchemy import select

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.audit import AuditEvent
from app.models.base import User
from app.models.redemption import RedemptionPartner, RedemptionBatch, RedemptionCode, CodeStatus


async def seed_codes():
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        admin = User(username=f"staff_{suffix}", email=f"staff_{suffix}@test.com",
                     casdoor_id=f"staff_{suffix}", is_superuser=True)
        buyer = User(username=f"buyer_{suffix}", email=f"buyer_{suffix}@test.com",
                     casdoor_id=f"buyer_{suffix}", cash=Decimal("100"))
        db.add_all([admin, buyer])
        await db.flush()
        partner = RedemptionPartner(name="线下活动")
        db.add(partner)
        await db.flush()
        batch = RedemptionBatch(partner_id=partner.id, name="现场礼品", unit_price=Decimal("10"), status="active")
        other_batch = RedemptionBatch(partner_id=partner.id, name="另一批次", unit_price=Decimal("10"))
        db.add_all([batch, other_batch])
        await db.flush()
        available = RedemptionCode(batch_id=batch.id, code_string="thfgavailable001")
        sold = RedemptionCode(batch_id=batch.id, code_string="thfgsold002", status=CodeStatus.SOLD,
                              bought_by_user_id=buyer.id, bought_at=datetime.now(timezone.utc))
        literal = RedemptionCode(batch_id=batch.id, code_string="thfg_ab%003", status=CodeStatus.SOLD,
                                 bought_by_user_id=buyer.id, bought_at=datetime.now(timezone.utc))
        wildcard = RedemptionCode(batch_id=batch.id, code_string="thfgXabY003", status=CodeStatus.SOLD,
                                  bought_by_user_id=buyer.id, bought_at=datetime.now(timezone.utc))
        db.add_all([available, sold, literal, wildcard])
        await db.commit()
        return {
            "admin_id": admin.id, "buyer_id": buyer.id, "batch_id": batch.id,
            "other_batch_id": other_batch.id, "available_id": available.id, "sold_id": sold.id,
            "admin": {"Authorization": f"Bearer {create_access_token(admin.id)}"},
            "buyer": {"Authorization": f"Bearer {create_access_token(buyer.id)}"},
        }


@pytest.mark.asyncio
async def test_admin_lists_codes_with_filters_and_pagination(client):
    data = await seed_codes()
    url = "/api/v1/admin/redemption/codes"
    first = await client.get(url, params={"page_size": 2}, headers=data["admin"])
    assert first.status_code == 200, first.text
    assert first.json()["total"] == 4
    assert len(first.json()["items"]) == 2
    second = await client.get(url, params={"page_size": 2, "page": 2}, headers=data["admin"])
    assert second.status_code == 200
    assert {row["id"] for row in first.json()["items"]}.isdisjoint(row["id"] for row in second.json()["items"])
    pending = await client.get(url, params={"status": "pending"}, headers=data["admin"])
    assert pending.json()["total"] == 3
    assert all(row["status"] == "sold" and row["redeemed_at"] is None for row in pending.json()["items"])
    unsold = await client.get(url, params={"status": "available"}, headers=data["admin"])
    assert [row["code_string"] for row in unsold.json()["items"]] == ["thfgavailable001"]
    other = await client.get(url, params={"batch_id": data["other_batch_id"]}, headers=data["admin"])
    assert other.json()["items"] == []
    missing = await client.get(url, params={"batch_id": 999999}, headers=data["admin"])
    assert missing.status_code == 404
    bad_filter = await client.get(url, params={"status": "unknown"}, headers=data["admin"])
    assert bad_filter.status_code == 422


@pytest.mark.asyncio
async def test_search_treats_wildcards_literally_and_ignores_case(client):
    data = await seed_codes()
    response = await client.get("/api/v1/admin/redemption/codes", params={"q": "  _AB%  "}, headers=data["admin"])
    assert response.status_code == 200
    assert [row["code_string"] for row in response.json()["items"]] == ["thfg_ab%003"]


@pytest.mark.asyncio
async def test_only_administrators_can_list_and_fulfill_codes(client):
    data = await seed_codes()
    list_url = "/api/v1/admin/redemption/codes"
    fulfill_url = f"{list_url}/{data['sold_id']}/redeem"
    assert (await client.get(list_url)).status_code in (401, 403)
    assert (await client.get(list_url, headers=data["buyer"])).status_code == 403
    assert (await client.post(fulfill_url, json={}, headers=data["buyer"])).status_code == 403
    assert (await client.post(f"{list_url}/{data['sold_id']}/revoke", json={"reason": "错误"}, headers=data["buyer"])).status_code == 403


@pytest.mark.asyncio
async def test_fulfillment_records_staff_and_preserves_sale_and_balance(client):
    data = await seed_codes()
    response = await client.post(f"/api/v1/admin/redemption/codes/{data['sold_id']}/redeem",
                                 json={"note": "已发放徽章"}, headers=data["admin"])
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["redeemed_at"] is not None
    assert datetime.fromisoformat(result["redeemed_at"]).utcoffset().total_seconds() == 0
    assert result["redeemed_by_admin_id"] == data["admin_id"]
    assert result["redeemed_by_admin_username"].startswith("staff_")
    assert result["redemption_note"] == "已发放徽章"
    assert result["status"] == "sold"
    async with async_session_maker() as db:
        buyer = await db.get(User, data["buyer_id"])
        assert buyer.cash == Decimal("100")
        events = list((await db.execute(select(AuditEvent).where(AuditEvent.event_type == "redeem_fulfill"))).scalars())
        assert len(events) == 1
        assert events[0].operator_user_id == data["admin_id"]
        assert events[0].user_id == data["buyer_id"]
        assert events[0].ref_id == data["sold_id"]
        assert events[0].payload["note"] == "已发放徽章"
    batches = (await client.get("/api/v1/admin/redemption/batches", headers=data["admin"])).json()
    batch = next(row for row in batches if row["id"] == data["batch_id"])
    assert batch["redeemed_count"] == 1
    assert batch["available_count"] == 1
    assert batch["sold_count"] == 3


@pytest.mark.asyncio
async def test_duplicate_fulfillment_does_not_replace_record(client):
    data = await seed_codes()
    url = f"/api/v1/admin/redemption/codes/{data['sold_id']}/redeem"
    first = await client.post(url, json={"note": "第一次发放"}, headers=data["admin"])
    assert first.status_code == 200
    second = await client.post(url, json={"note": "第二次"}, headers=data["admin"])
    assert second.status_code == 409
    current = (await client.get("/api/v1/admin/redemption/codes", params={"q": "thfgsold002", "status": "redeemed"}, headers=data["admin"])).json()
    assert current["total"] == 1
    assert current["items"][0]["redeemed_at"] == first.json()["redeemed_at"]
    assert current["items"][0]["redemption_note"] == "第一次发放"
    async with async_session_maker() as db:
        events = list((await db.execute(select(AuditEvent).where(AuditEvent.event_type == "redeem_fulfill"))).scalars())
        assert len(events) == 1


@pytest.mark.asyncio
async def test_user_personal_mark_cannot_cancel_staff_fulfillment(client):
    data = await seed_codes()
    cid = data["sold_id"]
    fulfilled = await client.post(f"/api/v1/admin/redemption/codes/{cid}/redeem", json={}, headers=data["admin"])
    assert fulfilled.status_code == 200
    for used in (True, False):
        marked = await client.post(f"/api/v1/redemption/my/{cid}/mark-used", json={"used": used}, headers=data["buyer"])
        assert marked.status_code == 200
    detail = (await client.get(f"/api/v1/redemption/my/{cid}", headers=data["buyer"])).json()
    assert detail["marked_used_by_user_at"] is None
    assert detail["redeemed_at"] == fulfilled.json()["redeemed_at"]
    listing = (await client.get("/api/v1/redemption/my", headers=data["buyer"])).json()
    assert next(row for row in listing if row["code_id"] == cid)["redeemed_at"] is not None


@pytest.mark.asyncio
async def test_unsold_missing_and_unfulfilled_codes_cannot_be_fulfilled_or_revoked(client):
    data = await seed_codes()
    base = "/api/v1/admin/redemption/codes"
    assert (await client.post(f"{base}/{data['available_id']}/redeem", json={}, headers=data["admin"])).status_code == 409
    assert (await client.post(f"{base}/999999/redeem", json={}, headers=data["admin"])).status_code == 404
    assert (await client.post(f"{base}/{data['sold_id']}/revoke", json={"reason": "误操作", "expected_redeemed_at": datetime.now(timezone.utc).isoformat()}, headers=data["admin"])).status_code == 409


@pytest.mark.asyncio
async def test_revocation_requires_reason_and_keeps_audit_history(client):
    data = await seed_codes()
    base = f"/api/v1/admin/redemption/codes/{data['sold_id']}"
    first = await client.post(base + "/redeem", json={"note": "发放"}, headers=data["admin"])
    assert first.status_code == 200
    missing = await client.post(base + "/revoke", json={}, headers=data["admin"])
    assert missing.status_code == 422
    blank = await client.post(base + "/revoke", json={"reason": "   ", "expected_redeemed_at": first.json()["redeemed_at"]}, headers=data["admin"])
    assert blank.status_code in (400, 422)
    response = await client.post(base + "/revoke", json={"reason": "发放前误点击", "expected_redeemed_at": first.json()["redeemed_at"]}, headers=data["admin"])
    assert response.status_code == 200, response.text
    assert response.json()["redeemed_at"] is None
    assert response.json()["redeemed_by_admin_id"] is None
    assert response.json()["status"] == "sold"
    async with async_session_maker() as db:
        events = list((await db.execute(select(AuditEvent).where(AuditEvent.ref_table == "redemption_code").order_by(AuditEvent.id))).scalars())
        assert [event.event_type for event in events] == ["redeem_fulfill", "redeem_fulfill_revoke"]
        assert events[-1].payload["reason"] == "发放前误点击"
        assert events[-1].payload["previous_redeemed_at"] is not None
        assert events[-1].payload["previous_redeemed_by_admin_id"] == data["admin_id"]
    again = await client.post(base + "/redeem", json={}, headers=data["admin"])
    assert again.status_code == 200


@pytest.mark.asyncio
async def test_concurrent_fulfillment_has_only_one_success(client):
    data = await seed_codes()
    url = f"/api/v1/admin/redemption/codes/{data['sold_id']}/redeem"
    responses = await asyncio.gather(*[client.post(url, json={}, headers=data["admin"]) for _ in range(2)])
    assert sorted(response.status_code for response in responses) == [200, 409]
    async with async_session_maker() as db:
        events = list((await db.execute(select(AuditEvent).where(AuditEvent.event_type == "redeem_fulfill"))).scalars())
        assert len(events) == 1


@pytest.mark.asyncio
async def test_stale_revocation_cannot_clear_a_new_fulfillment(client):
    data = await seed_codes()
    base = f"/api/v1/admin/redemption/codes/{data['sold_id']}"
    first = await client.post(base + "/redeem", json={}, headers=data["admin"])
    assert first.status_code == 200
    expected = first.json()["redeemed_at"]
    revoked = await client.post(base + "/revoke", json={"reason": "纠正第一次", "expected_redeemed_at": expected}, headers=data["admin"])
    assert revoked.status_code == 200
    second = await client.post(base + "/redeem", json={"note": "重新确认已发放"}, headers=data["admin"])
    assert second.status_code == 200
    stale = await client.post(base + "/revoke", json={"reason": "旧弹窗", "expected_redeemed_at": expected}, headers=data["admin"])
    assert stale.status_code == 409
    current = await client.get("/api/v1/admin/redemption/codes", params={"q": "thfgsold002"}, headers=data["admin"])
    assert current.json()["items"][0]["redeemed_at"] == second.json()["redeemed_at"]
    assert current.json()["items"][0]["redemption_note"] == "重新确认已发放"
