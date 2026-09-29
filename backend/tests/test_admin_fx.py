from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.v1.admin_fx import router
from app.api.v1.fx import router as public_router
from app.core.database import get_async_session
from app.core.users import current_superuser
from app.models.audit import AuditEvent
from app.models.base import User
from app.models.fx import FxPair, FxTreasury
from tests.fx_test_helpers import fx_db


@pytest_asyncio.fixture
async def ctx(fx_db):
    admin = User(username="fx_admin", casdoor_id="fx_admin", is_superuser=True)
    normal = User(username="fx_normal", casdoor_id="fx_normal", is_superuser=False)
    fx_db.add_all([admin, normal]); await fx_db.commit()
    application = FastAPI()
    application.include_router(router, prefix="/api/v1/admin/fx")
    application.include_router(public_router, prefix="/api/v1/fx")

    async def session_override(): yield fx_db
    async def admin_override():
        user = application.state.current_user
        if not user.is_superuser: raise HTTPException(403, "Admin only")
        return user

    application.state.current_user = admin
    application.dependency_overrides[get_async_session] = session_override
    application.dependency_overrides[current_superuser] = admin_override
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
        yield client, fx_db, application, admin, normal


PAIR = {"currency_code": "USD", "currency_name": "Dollar", "status": "trading",
        "gold_reserve": "100", "foreign_reserve": "100", "target_price": "1",
        "initial_price": "1", "target_min": "0.5", "target_max": "2"}


@pytest.mark.asyncio
async def test_superuser_single_trading_pair_and_opened_identity(ctx):
    client, db, app, admin, normal = ctx
    app.state.current_user = normal
    assert (await client.post("/api/v1/admin/fx/pairs", json=PAIR)).status_code == 403
    assert (await client.get("/api/v1/admin/fx/events")).status_code == 403
    app.state.current_user = admin
    created = await client.post("/api/v1/admin/fx/pairs", json=PAIR)
    assert created.status_code == 200, created.text
    pair_id = created.json()["id"]
    assert (await client.post("/api/v1/admin/fx/pairs", json={**PAIR, "currency_code": "EUR"})).status_code == 409
    assert (await client.patch(f"/api/v1/admin/fx/pairs/{pair_id}", json={"currency_name": "New"})).status_code == 409
    assert (await client.patch(f"/api/v1/admin/fx/pairs/{pair_id}", json={"status": "paused"})).status_code == 200
    assert (await client.patch(f"/api/v1/admin/fx/pairs/{pair_id}", json={"currency_code": "EUR"})).status_code == 409
    assert (await db.get(FxPair, pair_id)).currency_code == "USD"


@pytest.mark.asyncio
async def test_initial_issuance_fund_withdraw_and_audit(ctx):
    client, db, _, admin, _ = ctx
    pair_id = (await client.post("/api/v1/admin/fx/pairs", json=PAIR)).json()["id"]
    pair = await db.get(FxPair, pair_id)
    treasury = (await db.execute(select(FxTreasury).where(FxTreasury.pair_id == pair_id))).scalars().one()
    before_gold = pair.gold_reserve + treasury.gold_balance
    before_foreign = pair.foreign_reserve + treasury.foreign_balance
    assert before_gold == before_foreign == Decimal("200")
    funded = await client.post(f"/api/v1/admin/fx/pairs/{pair_id}/fund", json={"gold_amount": "3", "foreign_amount": "2"})
    assert funded.status_code == 200, funded.text
    withdrawn = await client.post(f"/api/v1/admin/fx/pairs/{pair_id}/withdraw", json={"gold_amount": "1", "foreign_amount": "1"})
    assert withdrawn.status_code == 200, withdrawn.text
    await db.refresh(pair); await db.refresh(treasury)
    assert pair.gold_reserve + treasury.gold_balance == before_gold + Decimal("4")
    assert pair.foreign_reserve + treasury.foreign_balance == before_foreign + Decimal("2")
    assert (await client.post(f"/api/v1/admin/fx/pairs/{pair_id}/withdraw", json={"gold_amount": "10000", "foreign_amount": "0"})).status_code == 409
    assert (await client.post(f"/api/v1/admin/fx/pairs/{pair_id}/fund", json={"gold_amount": "0.0000001", "foreign_amount": "0"})).status_code == 422
    audits = (await db.execute(select(AuditEvent).where(AuditEvent.ref_table == "fx_pair", AuditEvent.ref_id == pair_id))).scalars().all()
    assert {a.event_type for a in audits} >= {"fx_fund", "fx_withdraw"}
    assert all(a.operator_user_id == admin.id for a in audits)
    assert any(a.payload.get("action") == "initial_issuance" for a in audits)
    for event_type in ("fx_fund", "fx_withdraw"):
        row = next(a for a in audits if a.event_type == event_type and "pool_before" in a.payload)
        for section in ("pool_before", "pool_after", "treasury_before", "treasury_after"):
            assert set(row.payload[section]) == {"gold", "foreign"}


@pytest.mark.asyncio
async def test_event_ranges_state_and_public_boundary(ctx):
    client, _, _, _, _ = ctx
    pair_id = (await client.post("/api/v1/admin/fx/pairs", json=PAIR)).json()["id"]
    body = {"pair_id": pair_id, "title": "Policy shift", "body": "Rates changed", "kind": "macro",
            "shock_ratio": "0.001", "first_reaction_ratio": "0.25", "window_sec": 180, "budget": "10"}
    for invalid in ({"window_sec": 29}, {"shock_ratio": "0.06"}, {"first_reaction_ratio": "0.95"}, {"first_reaction_ratio": "-0.25"}, {"budget": "Infinity"}):
        response = await client.post("/api/v1/admin/fx/events", json={**body, **invalid})
        assert response.status_code == 422, response.text
    event = await client.post("/api/v1/admin/fx/events", json=body)
    assert event.status_code == 200, event.text
    assert "shock_ratio" in event.json()
    assert "shock_ratio" not in (await client.get("/api/v1/fx/pairs")).text
    cancelled = await client.post(f"/api/v1/admin/fx/events/{event.json()['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_scheduled_event_windows_cannot_overlap(ctx):
    client, _, _, _, _ = ctx
    pair_id = (await client.post("/api/v1/admin/fx/pairs", json=PAIR)).json()["id"]
    base = {"pair_id": pair_id, "title": "Scheduled", "kind": "macro", "shock_ratio": "0.01",
            "first_reaction_ratio": "0.25", "window_sec": 180, "budget": "10"}
    first = await client.post("/api/v1/admin/fx/events", json={**base, "scheduled_at": "2030-01-01T00:00:00Z"})
    assert first.status_code == 200, first.text
    overlap = await client.post("/api/v1/admin/fx/events", json={**base, "title": "Overlap", "scheduled_at": "2030-01-01T00:02:00Z"})
    assert overlap.status_code == 409, overlap.text


@pytest.mark.asyncio
async def test_config_gate_validation(ctx):
    client, _, _, _, _ = ctx
    assert (await client.put("/api/v1/admin/fx/config", json={"key": "fx_enabled", "value": "bad"})).status_code == 422
    assert (await client.put("/api/v1/admin/fx/config", json={"key": "fx_enabled", "value": "false"})).status_code == 200
    assert (await client.get("/api/v1/admin/fx/config")).json()["fx_enabled"] == "false"


@pytest.mark.asyncio
async def test_admin_read_lists_drafts_with_treasury_and_stays_superuser_only(ctx):
    client, db, app, admin, normal = ctx
    # Non-superusers cannot read the operator model.
    app.state.current_user = normal
    assert (await client.get("/api/v1/admin/fx/pairs")).status_code == 403

    app.state.current_user = admin
    created = await client.post("/api/v1/admin/fx/pairs",
                                json={**PAIR, "status": "draft", "currency_code": "DRF"})
    assert created.status_code == 200, created.text
    pair_id = created.json()["id"]

    listing = await client.get("/api/v1/admin/fx/pairs")
    assert listing.status_code == 200, listing.text
    rows = listing.json()
    assert [row["id"] for row in rows] == [pair_id]
    row = rows[0]
    # Draft pairs and their treasury budget are readable right after creation.
    assert row["status"] == "draft"
    assert Decimal(row["gold_reserve"]) == Decimal("100")
    assert Decimal(row["gold_balance"]) == Decimal("100")
    assert Decimal(row["foreign_balance"]) == Decimal("100")
    assert Decimal(row["daily_spend"]) == Decimal("0")
    assert "spend_date" in row

    # The public list still hides drafts.
    assert (await client.get("/api/v1/fx/pairs")).json() == []


@pytest.mark.asyncio
async def test_fee_rate_must_be_strictly_below_one(ctx):
    """M2: a 100% fee is rejected because the AMM would consume all input."""
    client, _, _, _, _ = ctx
    created = await client.post("/api/v1/admin/fx/pairs",
                                json={**PAIR, "buy_fee_rate": "0.01", "sell_fee_rate": "0.01"})
    assert created.status_code == 200, created.text
    pair_id = created.json()["id"]

    for field in ("buy_fee_rate", "sell_fee_rate"):
        rejected_create = await client.post(
            "/api/v1/admin/fx/pairs",
            json={**PAIR, "currency_code": "BAD1", field: "1"})
        assert rejected_create.status_code == 422, rejected_create.text
        rejected_patch = await client.patch(
            f"/api/v1/admin/fx/pairs/{pair_id}", json={field: "1"})
        assert rejected_patch.status_code == 422, rejected_patch.text

    # The largest 8-decimal rate strictly below 1 is still accepted.
    accepted = await client.patch(f"/api/v1/admin/fx/pairs/{pair_id}",
                                  json={"buy_fee_rate": "0.99999999"})
    assert accepted.status_code == 200, accepted.text
    assert Decimal(accepted.json()["buy_fee_rate"]) == Decimal("0.99999999")


@pytest.mark.asyncio
async def test_manual_publish_delegates_to_shared_event_service(ctx, monkeypatch):
    client, db, _, _, _ = ctx
    async def no_publish(_trade): return None
    monkeypatch.setattr("app.services.fx.market_data.publish_trade", no_publish)
    pair_id = (await client.post("/api/v1/admin/fx/pairs", json=PAIR)).json()["id"]
    event = await client.post("/api/v1/admin/fx/events", json={
        "pair_id": pair_id, "title": "Policy shift", "kind": "macro",
        "shock_ratio": "0.02", "first_reaction_ratio": "0.25", "window_sec": 180, "budget": "10",
    })
    assert event.status_code == 200, event.text
    published = await client.post(f"/api/v1/admin/fx/events/{event.json()['id']}/publish")
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published"
    assert published.json()["parameter_snapshot"]["first_trade_id"]
    pair = await db.get(FxPair, pair_id)
    assert pair.target_price == Decimal("1.02")
    retry = await client.post(f"/api/v1/admin/fx/events/{event.json()['id']}/publish")
    assert retry.status_code == 200
    assert retry.json()["parameter_snapshot"]["first_trade_id"] == published.json()["parameter_snapshot"]["first_trade_id"]
