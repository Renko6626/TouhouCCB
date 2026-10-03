from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.fx_test_helpers import add_pair, fx_db


@pytest.mark.asyncio
async def test_news_history_is_complete_public_and_pair_scoped(fx_db):
    from app.api.v1.fx import router
    from app.core.database import get_async_session
    from app.models.fx import FxEvent

    pair, _ = await add_pair(fx_db)
    other, _ = await add_pair(fx_db, code="OTHER")
    now = datetime.now(timezone.utc)
    fx_db.add_all([
        FxEvent(pair_id=pair.id, title=f"News {i}", body="Public body", kind="macro",
                status="published" if i == 6 else "completed",
                published_at=now + timedelta(minutes=i), shock_ratio="0.01",
                parameter_snapshot={"target_after": "1.2"})
        for i in range(7)
    ] + [
        FxEvent(pair_id=pair.id, title=status, kind="macro", status=status,
                published_at=now if status == "cancelled" else None)
        for status in ("draft", "scheduled", "cancelled")
    ] + [FxEvent(pair_id=other.id, title="Other pair", kind="macro",
                 status="published", published_at=now)])
    await fx_db.commit()
    application = FastAPI()
    application.include_router(router, prefix="/fx")

    async def session_override():
        yield fx_db

    application.dependency_overrides[get_async_session] = session_override
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
        response = await client.get(f"/fx/pairs/{pair.id}/news")
        assert response.status_code == 200
        news = response.json()
        assert [item["title"] for item in news] == [f"News {i}" for i in reversed(range(7))]
        assert all(item["body"] == "Public body" for item in news)
        assert all(set(item) == {"id", "pair_id", "status", "title", "body", "kind",
                                 "published_at", "completed_at"} for item in news)
        assert (await client.get("/fx/pairs/999999/news")).status_code == 404


def test_fx_router_exports_required_paths():
    from app.api.v1.fx import router
    paths = {route.path for route in router.routes}
    assert "/pairs" in paths
    assert "/pairs/{pair_id}/snapshot" in paths
    assert "/pairs/{pair_id}/quote" in paths
    assert "/pairs/{pair_id}/trades" in paths
    # I5 player reads: per-user wallet and personal trade history.
    assert "/pairs/{pair_id}/wallet" in paths
    assert "/pairs/{pair_id}/my-trades" in paths
    # Task 3c1 authenticated short writes (quote/current-position reads are 3c2).
    assert "/pairs/{pair_id}/short/open" in paths
    assert "/pairs/{pair_id}/short/cover" in paths


def test_admin_fx_router_exports_pair_read():
    from app.api.v1.admin_fx import router
    paths = {route.path for route in router.routes}
    assert "/pairs" in paths


def test_fx_request_schemas_enforce_six_decimal_places():
    from pydantic import ValidationError
    from app.schemas.fx import FxQuoteRequest, FxTradeRequest

    assert FxQuoteRequest(side="buy", amount="1.000000").amount == 1
    assert FxTradeRequest(side="buy", amount="1", min_out="0.000000", idempotency_key="k")
    for kwargs in ({"side": "buy", "amount": "1.0000001"},
                   {"side": "buy", "amount": "1", "min_out": "0.0000001", "idempotency_key": "k"}):
        with __import__("pytest").raises(ValidationError):
            (FxQuoteRequest(**kwargs) if "min_out" not in kwargs else FxTradeRequest(**kwargs))
