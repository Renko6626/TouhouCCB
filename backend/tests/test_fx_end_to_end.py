"""FX end-to-end behavior tests on isolated SQLite plus small ASGI overrides.

These tests are deliberately self-contained: they run with ``--noconftest`` and
do not depend on the repository-wide module-scoped client fixture.  Every test
builds a real FastAPI ASGI application, overrides only ``get_async_session`` so
requests read/write one isolated in-memory SQLite database, and uses *real* JWT
access tokens so the production ``current_active_user`` / ``current_superuser``
dependencies execute unchanged.

No assertion here reads source text or a helper's return value.  The evidence is
the HTTP response, the committed SQLite rows, and the real SSE broker payload.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, delete, func, select
from sqlmodel import Session, SQLModel

from app.api.v1 import fx_stream
from app.api.v1.admin_fx import router as admin_fx_router
from app.api.v1.fx import router as fx_router
from app.core.database import get_async_session
from app.core.users import create_access_token
from app.models.audit import AuditEvent
from app.models.base import SiteConfig, User
from app.models.fx import FxEvent, FxPair, FxTrade, FxTreasury, FxWallet
from app.schemas.fx import FxPairPublic, FxQuote, FxSnapshot, FxTradePublic, FxWalletPublic
from app.services import site_config
from app.services.fx import market_data, scheduler
from app.services.fx.amm import marginal_price
from app.services.fx.engine import FxEngine
from app.services.realtime import MarketEventBroker
from tests.fx_test_helpers import fx_db  # noqa: F401  (imported fixture)


def test_audit_json_sanitizes_date_and_preserves_datetime():
    from app.services.audit_service import _j
    assert _j(datetime(2026, 9, 28, 12, 34, 56, tzinfo=timezone.utc)) == "2026-09-28T12:34:56+00:00"
    assert _j(date(2026, 9, 28)) == "2026-09-28"

PAIR_PAYLOAD = {
    "currency_code": "USD",
    "currency_name": "Dollar",
    "status": "trading",
    "gold_reserve": "1000",
    "foreign_reserve": "1000",
    "target_price": "1",
    "initial_price": "1",
    "target_min": "0.5",
    "target_max": "2",
    "buy_fee_rate": "0.01",
    "sell_fee_rate": "0.01",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def _decimal(payload: dict, key: str) -> Decimal:
    return Decimal(str(payload[key]))


@pytest_asyncio.fixture
async def ctx(fx_db, monkeypatch):
    """Real ASGI app over one isolated SQLite session with production auth."""
    now = _utcnow()
    admin = User(username="fx-admin", casdoor_id="fx-admin", is_superuser=True,
                 cash=Decimal("0"), tos_accepted_at=now)
    normal = User(username="fx-normal", casdoor_id="fx-normal", is_superuser=False,
                  cash=Decimal("1000"), tos_accepted_at=now)
    trader = User(username="fx-trader", casdoor_id="fx-trader", is_superuser=False,
                  cash=Decimal("1000"), tos_accepted_at=now)
    bot = User(username="fx-bot", casdoor_id="fx-bot", is_superuser=False, is_bot=True,
               cash=Decimal("1000"), tos_accepted_at=now)
    no_tos = User(username="fx-notos", casdoor_id="fx-notos", cash=Decimal("1000"))
    debtor = User(username="fx-debtor", casdoor_id="fx-debtor", cash=Decimal("100"),
                  debt=Decimal("25"), tos_accepted_at=now)
    fx_db.add_all([admin, normal, trader, bot, no_tos, debtor])
    await fx_db.commit()

    application = FastAPI()
    application.include_router(fx_router, prefix="/api/v1/fx")
    application.include_router(fx_stream.router, prefix="/api/v1/fx")
    application.include_router(admin_fx_router, prefix="/api/v1/admin/fx")

    async def session_override():
        yield fx_db

    application.dependency_overrides[get_async_session] = session_override

    # Post-commit SSE publication reads its own session from app.core.database;
    # point it at the isolated database so a real trade emits a real frame.
    monkeypatch.setattr("app.core.database.async_session_maker", lambda: fx_db)

    async with AsyncClient(transport=ASGITransport(app=application),
                           base_url="http://fx.test") as client:
        yield SimpleNamespace(client=client, db=fx_db, app=application, admin=admin,
                              normal=normal, trader=trader, bot=bot, no_tos=no_tos,
                              debtor=debtor)


async def _create_pair(ctx, **overrides) -> int:
    response = await ctx.client.post("/api/v1/admin/fx/pairs",
                                     json={**PAIR_PAYLOAD, **overrides},
                                     headers=_auth(ctx.admin))
    assert response.status_code == 200, response.text
    return int(response.json()["id"])


@pytest.mark.asyncio
async def test_admin_routes_require_superuser_and_trades_require_real_token(ctx):
    client, admin, normal = ctx.client, ctx.admin, ctx.normal

    # Public market reads stay open; the seeded pair is created after.
    assert (await client.get("/api/v1/fx/pairs")).status_code == 200

    # A genuine non-superuser JWT is rejected by the production dependency.
    forbidden_read = await client.get("/api/v1/admin/fx/events", headers=_auth(normal))
    assert forbidden_read.status_code == 403
    forbidden_write = await client.post("/api/v1/admin/fx/pairs", json=PAIR_PAYLOAD,
                                        headers=_auth(normal))
    assert forbidden_write.status_code == 403
    assert (await ctx.db.execute(select(func.count()).select_from(FxPair))).scalar_one() == 0

    pair_id = await _create_pair(ctx)

    # No credential at all cannot trade.
    unauthenticated = await client.post(
        f"/api/v1/fx/pairs/{pair_id}/trades",
        json={"side": "buy", "amount": "1", "min_out": "0", "idempotency_key": "no-auth"},
    )
    assert unauthenticated.status_code in (401, 403)

    # The same admin JWT succeeds on the admin surface.
    admin_events = await client.get("/api/v1/admin/fx/events", headers=_auth(admin))
    assert admin_events.status_code == 200
    assert admin_events.json() == []


@pytest.mark.asyncio
async def test_fx_gate_is_closed_without_explicit_config_row(ctx):
    pair_id = await _create_pair(ctx)
    await ctx.db.execute(delete(SiteConfig).where(SiteConfig.key == "fx_enabled"))
    await ctx.db.commit()
    site_config.clear_cache()

    response = await ctx.client.post(
        f"/api/v1/fx/pairs/{pair_id}/trades",
        json={"side": "buy", "amount": "1", "min_out": "0", "idempotency_key": "gate-off"},
        headers=_auth(ctx.trader),
    )
    assert response.status_code == 403
    assert (await ctx.db.execute(select(func.count()).select_from(FxTrade))).scalar_one() == 0


@pytest.mark.asyncio
async def test_tos_bot_and_debt_guards_are_enforced_over_http(ctx):
    client, db = ctx.client, ctx.db
    pair_id = await _create_pair(ctx)

    async def attempt(user, side, amount, key):
        return await client.post(
            f"/api/v1/fx/pairs/{pair_id}/trades",
            json={"side": side, "amount": amount, "min_out": "0", "idempotency_key": key},
            headers=_auth(user),
        )

    # Missing TOS acceptance blocks a buy and leaves no partial wallet behind.
    no_tos = await attempt(ctx.no_tos, "buy", "5", "tos-buy")
    assert no_tos.status_code == 403
    assert "TOS" in no_tos.json()["detail"]
    assert (await db.execute(select(FxWallet).where(FxWallet.user_id == ctx.no_tos.id))).scalars().all() == []
    await db.refresh(ctx.no_tos)
    assert ctx.no_tos.cash == Decimal("1000")

    # Bots are blocked on both sides even with cash and TOS.
    bot_buy = await attempt(ctx.bot, "buy", "5", "bot-buy")
    bot_sell = await attempt(ctx.bot, "sell", "5", "bot-sell")
    assert bot_buy.status_code == 403 and "bot" in bot_buy.json()["detail"]
    assert bot_sell.status_code == 403 and "bot" in bot_sell.json()["detail"]

    # Debt blocks buying...
    debt_buy = await attempt(ctx.debtor, "buy", "5", "debt-buy")
    assert debt_buy.status_code == 403
    assert "debt" in debt_buy.json()["detail"]

    # ...but an existing foreign balance can still be sold back for gold.
    db.add(FxWallet(user_id=ctx.debtor.id, pair_id=pair_id,
                    foreign_amount=Decimal("5"), cost_basis=Decimal("5")))
    await db.commit()
    before_cash = (await db.get(User, ctx.debtor.id)).cash
    debt_sell = await attempt(ctx.debtor, "sell", "1", "debt-sell")
    assert debt_sell.status_code == 200, debt_sell.text
    await db.refresh(ctx.debtor)
    wallet = (await db.execute(select(FxWallet).where(
        FxWallet.user_id == ctx.debtor.id, FxWallet.pair_id == pair_id))).scalars().one()
    assert ctx.debtor.cash > before_cash
    assert wallet.foreign_amount == Decimal("4")


@pytest.mark.asyncio
async def test_quote_trade_stale_min_out_and_idempotent_replay(ctx):
    client, db = ctx.client, ctx.db
    pair_id = await _create_pair(ctx)
    trader = ctx.trader
    headers = _auth(trader)

    quote_response = await client.post(f"/api/v1/fx/pairs/{pair_id}/quote",
                                       json={"side": "buy", "amount": "10"}, headers=headers)
    assert quote_response.status_code == 200, quote_response.text
    quote = quote_response.json()
    quoted_out = _decimal(quote, "output_amount")
    assert _decimal(quote, "input_amount") == Decimal("10")
    assert _decimal(quote, "fee_amount") == Decimal("0.100000")
    assert quoted_out > 0

    trade_body = {"side": "buy", "amount": "10", "min_out": str(quoted_out),
                  "idempotency_key": "order-1"}
    first = await client.post(f"/api/v1/fx/pairs/{pair_id}/trades", json=trade_body, headers=headers)
    assert first.status_code == 200, first.text
    trade_id = first.json()["id"]
    assert _decimal(first.json(), "output_amount") == quoted_out

    await db.refresh(trader)
    wallet = (await db.execute(select(FxWallet).where(
        FxWallet.user_id == trader.id, FxWallet.pair_id == pair_id))).scalars().one()
    assert trader.cash == Decimal("990")
    assert wallet.foreign_amount == quoted_out

    # Exact idempotent replay returns the same committed trade with no new debit.
    replay = await client.post(f"/api/v1/fx/pairs/{pair_id}/trades", json=trade_body, headers=headers)
    assert replay.status_code == 200
    assert replay.json()["id"] == trade_id
    await db.refresh(trader)
    await db.refresh(wallet)
    assert trader.cash == Decimal("990") and wallet.foreign_amount == quoted_out

    # Same key, different parameters is a conflict.
    mismatch = await client.post(
        f"/api/v1/fx/pairs/{pair_id}/trades",
        json={**trade_body, "amount": "11"}, headers=headers)
    assert mismatch.status_code == 409
    mismatch_min = await client.post(
        f"/api/v1/fx/pairs/{pair_id}/trades",
        json={**trade_body, "min_out": str(quoted_out - Decimal("0.000001"))}, headers=headers)
    assert mismatch_min.status_code == 409

    # The pool moved up after the first buy, so the original min_out is now stale.
    stale = await client.post(
        f"/api/v1/fx/pairs/{pair_id}/trades",
        json={**trade_body, "idempotency_key": "order-2"}, headers=headers)
    assert stale.status_code == 409
    await db.refresh(trader)
    assert trader.cash == Decimal("990")
    assert (await db.execute(select(func.count()).select_from(FxTrade))).scalar_one() == 1

    # Seven fractional digits are rejected at the schema boundary.
    too_precise = await client.post(
        f"/api/v1/fx/pairs/{pair_id}/trades",
        json={"side": "buy", "amount": "1.0000001", "min_out": "0",
              "idempotency_key": "order-3"}, headers=headers)
    assert too_precise.status_code == 422


@pytest.mark.asyncio
async def test_public_api_allowlist_hides_targets_shocks_and_wallets(ctx):
    client = ctx.client
    pair_id = await _create_pair(ctx, target_price="1.25", target_min="0.5", target_max="2")
    headers = _auth(ctx.trader)

    # A hidden event carries shock / first-reaction / future values in the DB.
    event = await client.post("/api/v1/admin/fx/events", json={
        "pair_id": pair_id, "title": "Hidden shock", "body": "internal",
        "kind": "macro", "shock_ratio": "0.03", "first_reaction_ratio": "0.25",
        "window_sec": 180, "budget": "50"}, headers=_auth(ctx.admin))
    assert event.status_code == 200, event.text
    assert "shock_ratio" in event.json()

    buy = await client.post(f"/api/v1/fx/pairs/{pair_id}/trades",
                            json={"side": "buy", "amount": "5", "min_out": "0",
                                  "idempotency_key": "allowlist-buy"}, headers=headers)
    assert buy.status_code == 200, buy.text

    hidden = {"gold_reserve", "foreign_reserve", "target_price", "target_min", "target_max",
              "initial_price", "buy_fee_rate", "sell_fee_rate", "shock_ratio",
              "first_reaction_ratio", "parameter_snapshot", "idempotency_key", "min_out",
              "user_id", "source", "pre_gold_reserve", "pre_foreign_reserve",
              "post_gold_reserve", "post_foreign_reserve"}

    pairs = (await client.get("/api/v1/fx/pairs")).json()
    assert len(pairs) == 1
    assert set(pairs[0]) == set(FxPairPublic.model_fields)
    assert hidden.isdisjoint(pairs[0])

    snapshot = (await client.get(f"/api/v1/fx/pairs/{pair_id}/snapshot")).json()
    assert set(snapshot) == set(FxSnapshot.model_fields)
    assert hidden.isdisjoint(snapshot)

    quote = (await client.post(f"/api/v1/fx/pairs/{pair_id}/quote",
                               json={"side": "buy", "amount": "1"}, headers=headers)).json()
    assert set(quote) == set(FxQuote.model_fields)

    trades = (await client.get(f"/api/v1/fx/pairs/{pair_id}/trades")).json()
    assert len(trades) == 1
    assert set(trades[0]) == set(FxTradePublic.model_fields)
    assert hidden.isdisjoint(trades[0])

    now = _utcnow()
    chart = await client.get(
        f"/api/v1/fx/pairs/{pair_id}/chart",
        params={"interval": "1m",
                "from": (now - timedelta(minutes=5)).isoformat(),
                "to": (now + timedelta(minutes=5)).isoformat()})
    assert chart.status_code == 200, chart.text
    candles = chart.json()
    assert len(candles) >= 1
    assert set(candles[0]) == {"bucket_start", "interval", "open", "high", "low", "close", "volume"}
    assert hidden.isdisjoint(candles[0])

    # The operator view must still surface the private parameters.
    admin_events = (await client.get("/api/v1/admin/fx/events", headers=_auth(ctx.admin))).json()
    assert admin_events and "shock_ratio" in admin_events[0]
    assert admin_events[0]["parameter_snapshot"] is None


@pytest.mark.asyncio
async def test_player_wallet_and_personal_trades_are_scoped_to_owner(ctx):
    client, db = ctx.client, ctx.db
    pair_id = await _create_pair(ctx)

    # A user who has never traded gets a zeroed wallet (not a 404).
    empty = await client.get(f"/api/v1/fx/pairs/{pair_id}/wallet", headers=_auth(ctx.trader))
    assert empty.status_code == 200, empty.text
    assert set(empty.json()) == set(FxWalletPublic.model_fields)
    assert _decimal(empty.json(), "foreign_amount") == 0
    assert _decimal(empty.json(), "cost_basis") == 0
    assert empty.json()["updated_at"] is None

    # Wallet and personal history require authentication.
    assert (await client.get(f"/api/v1/fx/pairs/{pair_id}/wallet")).status_code in (401, 403)
    assert (await client.get(f"/api/v1/fx/pairs/{pair_id}/my-trades")).status_code in (401, 403)

    trader_trade = await client.post(
        f"/api/v1/fx/pairs/{pair_id}/trades",
        json={"side": "buy", "amount": "5", "min_out": "0", "idempotency_key": "mine-1"},
        headers=_auth(ctx.trader))
    normal_trade = await client.post(
        f"/api/v1/fx/pairs/{pair_id}/trades",
        json={"side": "buy", "amount": "3", "min_out": "0", "idempotency_key": "theirs-1"},
        headers=_auth(ctx.normal))
    assert trader_trade.status_code == 200, trader_trade.text
    assert normal_trade.status_code == 200, normal_trade.text

    wallet = (await client.get(f"/api/v1/fx/pairs/{pair_id}/wallet",
                               headers=_auth(ctx.trader))).json()
    assert _decimal(wallet, "foreign_amount") == _decimal(trader_trade.json(), "output_amount")
    assert _decimal(wallet, "cost_basis") == Decimal("5")
    assert wallet["updated_at"] is not None

    # Personal history contains only the caller's trades; the public feed still
    # shows both trades but never exposes a user identity.
    mine = (await client.get(f"/api/v1/fx/pairs/{pair_id}/my-trades",
                             headers=_auth(ctx.trader))).json()
    assert [row["id"] for row in mine] == [trader_trade.json()["id"]]
    assert set(mine[0]) == set(FxTradePublic.model_fields)
    public = (await client.get(f"/api/v1/fx/pairs/{pair_id}/trades")).json()
    assert len(public) == 2
    assert all("user_id" not in row for row in public)


@pytest.mark.asyncio
async def test_public_trade_feed_hides_system_source_but_admin_interventions_expose_it(ctx):
    """M1: internal system sources must never reach public trade payloads."""
    client, db = ctx.client, ctx.db
    pair_id = await _create_pair(ctx)
    pair = await db.get(FxPair, pair_id)
    treasury = (await db.execute(select(FxTreasury).where(
        FxTreasury.pair_id == pair_id))).scalars().one()

    # A real system intervention writes a stored trade with an internal source.
    engine = FxEngine()
    now = _utcnow()
    price = marginal_price(pair.gold_reserve, pair.foreign_reserve)
    moved = await engine._system_move(db, pair, treasury, price * Decimal("1.02"),
                                      Decimal("0"), Decimal("100000"),
                                      source="target", now=now)
    assert moved and moved.trade.source == "system_target", moved
    await db.commit()

    # A player trade shares the same public feed.
    traded = await client.post(
        f"/api/v1/fx/pairs/{pair_id}/trades",
        json={"side": "buy", "amount": "5", "min_out": "0",
              "idempotency_key": "public-feed-m1"},
        headers=_auth(ctx.trader))
    assert traded.status_code == 200, traded.text
    assert "source" not in traded.json()

    public = await client.get(f"/api/v1/fx/pairs/{pair_id}/trades")
    assert public.status_code == 200, public.text
    rows = public.json()
    assert {row["id"] for row in rows} >= {moved.trade.id, traded.json()["id"]}
    assert all("source" not in row for row in rows)
    serialized = json.dumps(rows)
    for internal in ("source", "system_target", "system_event", "system_noise"):
        assert internal not in serialized

    # Personal history uses the same shape and also hides the field.
    mine = await client.get(f"/api/v1/fx/pairs/{pair_id}/my-trades",
                            headers=_auth(ctx.trader))
    assert mine.status_code == 200, mine.text
    assert all("source" not in row for row in mine.json())

    # Operators keep the internal origin through the admin-only endpoint.
    interventions = await client.get(f"/api/v1/admin/fx/pairs/{pair_id}/interventions",
                                     headers=_auth(ctx.admin))
    assert interventions.status_code == 200, interventions.text
    assert any(row["source"] == "system_target" for row in interventions.json())


@pytest.mark.asyncio
async def test_stored_fee_rate_one_returns_actionable_http_not_500(ctx):
    """M2: pathological stored fee rate must 422, never crash the snapshot."""
    client, db = ctx.client, ctx.db
    pair_id = await _create_pair(ctx)
    pair = await db.get(FxPair, pair_id)
    pair.buy_fee_rate = Decimal("1")
    pair.sell_fee_rate = Decimal("1")
    await db.commit()

    # raise_app_exceptions=False so a regression to 500 is observable as HTTP.
    transport = ASGITransport(app=ctx.app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://fx.test") as raw:
        snapshot = await raw.get(f"/api/v1/fx/pairs/{pair_id}/snapshot")
        quote = await raw.post(f"/api/v1/fx/pairs/{pair_id}/quote",
                               json={"side": "buy", "amount": "1"})

    assert snapshot.status_code == 422, snapshot.text
    assert "fee" in snapshot.json()["detail"].lower()
    assert quote.status_code == 422, quote.text
    assert "fee" in quote.json()["detail"].lower()


@pytest.mark.asyncio
async def test_tiny_reserves_that_round_to_zero_return_actionable_http_not_500(ctx):
    """M2: reserves too small to round one unit must 422, not 500."""
    pair_id = await _create_pair(ctx, gold_reserve="0.000001",
                                 foreign_reserve="0.000001",
                                 buy_fee_rate="0", sell_fee_rate="0")

    transport = ASGITransport(app=ctx.app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://fx.test") as raw:
        snapshot = await raw.get(f"/api/v1/fx/pairs/{pair_id}/snapshot")
        quote = await raw.post(f"/api/v1/fx/pairs/{pair_id}/quote",
                               json={"side": "sell", "amount": "0.000001"})

    assert snapshot.status_code == 422, snapshot.text
    assert "invalid" in snapshot.json()["detail"].lower()
    assert quote.status_code == 422, quote.text


@pytest.mark.asyncio
async def test_sse_frames_only_expose_allowlisted_market_fields(ctx, monkeypatch):
    client, db = ctx.client, ctx.db
    pair_id = await _create_pair(ctx)
    buy = await client.post(f"/api/v1/fx/pairs/{pair_id}/trades",
                            json={"side": "buy", "amount": "5", "min_out": "0",
                                  "idempotency_key": "sse-buy"}, headers=_auth(ctx.trader))
    assert buy.status_code == 200, buy.text
    trade = (await db.execute(select(FxTrade).where(FxTrade.pair_id == pair_id))).scalars().one()

    allowed = {"price", "buy_price", "sell_price", "spread", "volume"}
    broker = MarketEventBroker()
    monkeypatch.setattr(fx_stream, "async_session_maker", lambda: db)
    monkeypatch.setattr(fx_stream, "BROKER", broker)
    request = SimpleNamespace(headers={"x-forwarded-for": "10.9.9.1"},
                              client=SimpleNamespace(host="10.9.9.1"))

    # 1) The endpoint's initial snapshot frame is produced from the real DB.
    response = await fx_stream.stream(pair_id, request)
    raw = await response.body_iterator.__anext__()
    await response.body_iterator.aclose()
    initial = json.loads(raw.decode().split("data: ", 1)[1])
    assert initial["type"] == "snapshot"
    assert set(initial["data"]) == allowed
    serialized = json.dumps(initial["data"])
    for secret in ("target_price", "shock_ratio", "future", "random", "parameter_snapshot"):
        assert secret not in serialized
    assert isinstance(initial["data"]["price"], str)
    assert isinstance(initial["data"]["volume"], str)

    # 2) A real committed trade publishes a frame with the same allowlist.
    sub, _ = await broker.subscribe(pair_id)
    try:
        await market_data.publish_trade(trade, broker)
        published = json.loads((await sub.q.get()).decode().split("data: ", 1)[1])
    finally:
        await broker.unsubscribe(pair_id, sub)
    assert set(published["data"]) == allowed
    for secret in ("target_price", "shock_ratio", "user_id", "source", "idempotency_key"):
        assert secret not in json.dumps(published["data"])


@pytest.mark.asyncio
async def test_scheduled_event_gate_off_blocks_publish(ctx, monkeypatch):
    """With the production gate off a due scheduled event must not run."""
    client, db = ctx.client, ctx.db
    pair_id = await _create_pair(ctx)
    scheduled_at = (_utcnow() - timedelta(seconds=10)).isoformat()
    created = await client.post("/api/v1/admin/fx/events", json={
        "pair_id": pair_id, "title": "Scheduled shock", "body": "rates",
        "kind": "macro", "shock_ratio": "0.01", "first_reaction_ratio": "0.25",
        "window_sec": 180, "budget": "40", "scheduled_at": scheduled_at},
        headers=_auth(ctx.admin))
    assert created.status_code == 200, created.text
    event_id = int(created.json()["id"])
    assert created.json()["status"] == "scheduled"

    monkeypatch.setattr(scheduler, "async_session_maker", lambda: db)
    monkeypatch.setattr(scheduler, "ENGINE", FxEngine(session_factory=lambda: db))

    off = await client.put("/api/v1/admin/fx/config",
                           json={"key": "fx_enabled", "value": "false"},
                           headers=_auth(ctx.admin))
    assert off.status_code == 200, off.text
    site_config.clear_cache()
    await scheduler._tick_safe()

    blocked = await db.get(FxEvent, event_id)
    await db.refresh(blocked)
    assert blocked.status == "scheduled"
    assert blocked.published_at is None
    assert (await db.execute(select(func.count()).select_from(FxTrade))).scalar_one() == 0


@pytest.mark.asyncio
async def test_scheduled_event_publishes_recovers_and_charges_only_event_spend(ctx, monkeypatch):
    client, db = ctx.client, ctx.db
    pair_id = await _create_pair(ctx)
    scheduled_at = (_utcnow() - timedelta(seconds=10)).isoformat()
    created = await client.post("/api/v1/admin/fx/events", json={
        "pair_id": pair_id, "title": "Scheduled shock", "body": "rates",
        "kind": "macro", "shock_ratio": "0.01", "first_reaction_ratio": "0.25",
        "window_sec": 180, "budget": "40", "scheduled_at": scheduled_at},
        headers=_auth(ctx.admin))
    assert created.status_code == 200, created.text
    event_id = int(created.json()["id"])
    assert created.json()["status"] == "scheduled"

    monkeypatch.setattr(scheduler, "async_session_maker", lambda: db)
    monkeypatch.setattr(scheduler, "ENGINE", FxEngine(session_factory=lambda: db))
    on = await client.put("/api/v1/admin/fx/config",
                          json={"key": "fx_enabled", "value": "true"},
                          headers=_auth(ctx.admin))
    assert on.status_code == 200, on.text
    site_config.clear_cache()

    # A real scheduler scan publishes the overdue event exactly once.
    await scheduler._tick_safe()
    saved = await db.get(FxEvent, event_id)
    await db.refresh(saved)
    assert saved.status == "published", saved.error_message
    first_trade_id = saved.parameter_snapshot["first_trade_id"]
    publishes = (await db.execute(select(func.count()).select_from(AuditEvent)
                                  .where(AuditEvent.event_type == "fx_event_publish"))).scalar_one()
    assert publishes == 1
    pair = await db.get(FxPair, pair_id)
    await db.refresh(pair)
    assert pair.target_price == Decimal("1.01")

    # Recovery: a fresh scan over the same DB state must not repeat first action.
    await scheduler._tick_safe()
    await db.refresh(saved)
    assert saved.parameter_snapshot["first_trade_id"] == first_trade_id
    assert (await db.execute(select(func.count()).select_from(AuditEvent)
                             .where(AuditEvent.event_type == "fx_event_publish"))).scalar_one() == 1

    # Advance the event window; the engine must charge only this event's spend.
    await scheduler.ENGINE.tick(now=_utcnow() + timedelta(seconds=90))
    await db.refresh(saved)
    await db.refresh(pair)
    treasury = (await db.execute(select(FxTreasury).where(
        FxTreasury.pair_id == pair_id))).scalars().one()

    event_buys = (await db.execute(select(FxTrade).where(
        FxTrade.pair_id == pair_id, FxTrade.source == "system_event",
        FxTrade.side == "buy"))).scalars().all()
    event_input_total = sum((t.input_amount for t in event_buys), Decimal("0"))
    spent = Decimal(str(saved.parameter_snapshot["spent"]))
    assert spent > 0
    assert spent == event_input_total
    assert spent <= Decimal("40")
    assert treasury.daily_spend >= spent
    assert (await db.execute(select(func.count()).select_from(FxTrade)
                             .where(FxTrade.source == "system_event"))).scalar_one() >= 2


@pytest.mark.asyncio
async def test_system_move_conserves_each_currency_and_fund_withdraw_totals(ctx):
    client, db = ctx.client, ctx.db
    pair_id = await _create_pair(ctx)
    pair = await db.get(FxPair, pair_id)
    treasury = (await db.execute(select(FxTreasury).where(
        FxTreasury.pair_id == pair_id))).scalars().one()
    gold_start = pair.gold_reserve + treasury.gold_balance
    foreign_start = pair.foreign_reserve + treasury.foreign_balance
    spend_start = treasury.daily_spend

    engine = FxEngine()
    now = _utcnow()
    price = marginal_price(pair.gold_reserve, pair.foreign_reserve)
    bought = await engine._system_move(db, pair, treasury, price * Decimal("1.02"),
                                       Decimal("0"), Decimal("100000"),
                                       source="e2e-buy", now=now)
    assert bought and bought.trade.side == "buy"
    await db.commit()
    await db.refresh(pair)
    await db.refresh(treasury)
    assert pair.gold_reserve + treasury.gold_balance == gold_start
    assert pair.foreign_reserve + treasury.foreign_balance == foreign_start

    price = marginal_price(pair.gold_reserve, pair.foreign_reserve)
    sold = await engine._system_move(db, pair, treasury, price * Decimal("0.98"),
                                     Decimal("0"), Decimal("100000"),
                                     source="e2e-sell", now=now)
    assert sold and sold.trade.side == "sell"
    await db.commit()
    await db.refresh(pair)
    await db.refresh(treasury)
    assert pair.gold_reserve + treasury.gold_balance == gold_start
    assert pair.foreign_reserve + treasury.foreign_balance == foreign_start
    spend_after_system = treasury.daily_spend
    assert spend_after_system > spend_start

    # Operator funding/withdrawal only move explicit issuance, not budget spend.
    fund = await client.post(f"/api/v1/admin/fx/pairs/{pair_id}/fund",
                             json={"gold_amount": "50", "foreign_amount": "60"},
                             headers=_auth(ctx.admin))
    withdraw = await client.post(f"/api/v1/admin/fx/pairs/{pair_id}/withdraw",
                                 json={"gold_amount": "10", "foreign_amount": "20"},
                                 headers=_auth(ctx.admin))
    assert fund.status_code == 200, fund.text
    assert withdraw.status_code == 200, withdraw.text
    await db.refresh(pair)
    await db.refresh(treasury)
    # `fund_pair` adds each amount to BOTH the pool and the treasury buffer, so
    # the per-currency system total grows by 2*fund - 2*withdraw.
    assert pair.gold_reserve + treasury.gold_balance == gold_start + Decimal("80")
    assert pair.foreign_reserve + treasury.foreign_balance == foreign_start + Decimal("80")
    assert treasury.daily_spend == spend_after_system

    # Funding/withdrawal cannot be performed by a normal player.
    denied = await client.post(f"/api/v1/admin/fx/pairs/{pair_id}/fund",
                               json={"gold_amount": "1", "foreign_amount": "0"},
                               headers=_auth(ctx.normal))
    assert denied.status_code == 403


class _ResetSession:
    """Async-shaped adapter over one synchronous SQLite transaction."""

    def __init__(self, session: Session):
        self._session = session

    def add(self, value): self._session.add(value)
    def add_all(self, values): self._session.add_all(values)
    async def execute(self, statement): return self._session.execute(statement)
    async def flush(self): self._session.flush()
    async def commit(self): self._session.commit()
    async def rollback(self): self._session.rollback()
    async def get(self, model, key): return self._session.get(model, key)
    def in_transaction(self): return self._session.in_transaction()

    def begin(self):
        session = self._session

        class _Begin:
            async def __aenter__(self_inner):
                self_inner.tx = session.begin()
                self_inner.tx.__enter__()
                return self_inner

            async def __aexit__(self_inner, exc_type, exc, tb):
                self_inner.tx.__exit__(exc_type, exc, tb)

        return _Begin()

    async def __aenter__(self): return self
    async def __aexit__(self, exc_type, exc, tb):
        self._session.rollback()
        self._session.close()


async def _count(session_factory, model) -> int:
    async with session_factory() as db:
        return int((await db.execute(select(func.count()).select_from(model))).scalar_one())


@pytest.mark.asyncio
async def test_season_reset_clears_fx_and_preserves_redemption_records(monkeypatch):
    from app.models.redemption import DanmukuExchange, RedemptionTransaction
    from app.services import audit_service
    from scripts import season_reset

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    sessions = lambda: _ResetSession(Session(engine))  # noqa: E731

    async with sessions() as db:
        async with db.begin():
            db.add_all([
                SiteConfig(key="initial_balance", value="500", value_type="decimal"),
                SiteConfig(key="fx_enabled", value="true", value_type="bool"),
            ])
            user = User(username="reset-user", casdoor_id="reset-user", cash=Decimal("12"))
            db.add(user)
            await db.flush()
            pair = FxPair(currency_code="RST", currency_name="Reset FX", status="trading",
                          gold_reserve=Decimal("100"), foreign_reserve=Decimal("100"))
            db.add(pair)
            await db.flush()
            db.add_all([
                FxWallet(user_id=user.id, pair_id=pair.id, foreign_amount=Decimal("2"),
                         cost_basis=Decimal("2")),
                FxTreasury(pair_id=pair.id, gold_balance=Decimal("3"), foreign_balance=Decimal("4")),
                FxTrade(pair_id=pair.id, user_id=user.id, side="buy",
                        input_amount=Decimal("1"), output_amount=Decimal("1"),
                        pre_gold_reserve=Decimal("100"), pre_foreign_reserve=Decimal("100"),
                        post_gold_reserve=Decimal("101"), post_foreign_reserve=Decimal("99"),
                        post_price=Decimal("1.02")),
                FxEvent(pair_id=pair.id, title="event", kind="macro"),
                RedemptionTransaction(user_id=user.id, amount=Decimal("5"),
                                      batch_name_snapshot="kept"),
                DanmukuExchange(user_id=user.id, qq_user_id="1", room_id="r",
                                yuan=Decimal("1"), huo=Decimal("1"),
                                amount=Decimal("2"), code_string="kept-code"),
            ])
            audit_service.record(db, "fx_trade", user_id=user.id, ref_table="fx_pair",
                                 ref_id=pair.id, payload={"pair_id": pair.id})
            audit_service.record(db, "redeem_purchase", user_id=user.id,
                                 payload={"amount": "5"},
                                 user_after={"cash": "7", "debt": "0"})
    monkeypatch.setattr(season_reset, "async_session_maker", sessions)

    # Dry run must be read-only: FX rows, gate and redemption rows all survive.
    assert await season_reset.run(dry_run=True) == 0
    for model in (FxWallet, FxTrade, FxEvent, FxTreasury, FxPair):
        assert await _count(sessions, model) == 1
    assert await _count(sessions, RedemptionTransaction) == 1
    assert await _count(sessions, DanmukuExchange) == 1

    monkeypatch.setattr("builtins.input", lambda *_: "RESET")
    assert await season_reset.run(dry_run=False) == 0

    for model in (FxWallet, FxTrade, FxEvent, FxTreasury, FxPair):
        assert await _count(sessions, model) == 0
    assert await _count(sessions, RedemptionTransaction) == 1
    assert await _count(sessions, DanmukuExchange) == 1
    async with sessions() as db:
        gate = (await db.execute(select(SiteConfig.value).where(
            SiteConfig.key == "fx_enabled"))).scalar_one()
        audit_types = [event.event_type for event in
                       (await db.execute(select(AuditEvent))).scalars().all()]
    assert gate == "false"
    assert all(not event_type.startswith("fx_") for event_type in audit_types)
    assert "redeem_purchase" in audit_types
    engine.dispose()
