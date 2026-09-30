"""Tasks 3c1/3c2: authenticated FX short write routes, read/quote APIs and gates.

Real HTTP + persisted-state scenarios against the FastAPI app, the real gate
registry, the real AMM and the real SQLite database.  They assert actual
cash / pool / treasury / wallet / short-debt / trade postconditions, never
source text or private helper names.

A regression here means: an operator can open shorts while the site gate or
pair lending cap is off; a same-key retry borrows/sells/buys twice or lets a
spot request replay a short trade (in either direction); a reduce-only pair
still lets an ordinary buy or a new short through; turning the opening gate or
the loan gate off strands an existing short with no cover path; a malformed or
overflowing request becomes a 500 or mutates the book; the admin pair limit
leaks into the public pair payload; the read route advances an interest clock or
shows another account's obligation; or a quote prices a different amount/cost
than the AMM or claims a reduce-only cover is disallowed.
"""
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING
from decimal import Decimal as D
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.audit import AuditEvent
from app.models.base import SiteConfig, User
from app.models.fx import FxPair, FxShortPosition, FxTrade, FxTreasury, FxWallet
from app.services import site_config
from app.services.credit import flags as credit_flags
from app.services.credit import risk as credit_risk
from app.services.credit.gates import GATES
from app.services.credit.valuation import value_user_detailed
from app.services.fx import publisher, shorts
from app.services.fx.amm import quote_buy_exact_out, quote_sell

pytestmark = pytest.mark.asyncio

ZERO = D("0")
Q6 = D("0.000001")

PAIR = {
    "currency_code": "USD", "currency_name": "Dollar", "status": "trading",
    "gold_reserve": "1000", "foreign_reserve": "1000", "target_price": "1",
    "initial_price": "1", "target_min": "0.5", "target_max": "2",
    "short_lending_limit_foreign": "0",
}

SPOT_TRADE_KEYS = {
    "id", "pair_id", "side", "input_amount", "output_amount",
    "fee_amount", "post_price", "created_at",
}


@pytest.fixture(autouse=True)
def _unified(monkeypatch):
    """Unified credit is process-global; the API test enables it in-process."""
    flags = credit_flags.parse_flags({
        "unified_credit_enabled": "true",
        "credit_leverage": "4",
        "credit_maintenance_ratio": "0.1",
        "credit_risk_retry_limit": "3",
    })
    monkeypatch.setattr(credit_flags, "get_flags", lambda: flags)
    site_config.clear_cache()
    yield
    site_config.clear_cache()


async def _make_user(*, cash="1000", superuser=False):
    suffix = uuid4().hex[:8]
    async with async_session_maker() as s:
        async with s.begin():
            user = User(username=f"short_{suffix}", casdoor_id=f"cd_{suffix}",
                        cash=D(cash), tos_accepted_at=datetime.now(timezone.utc),
                        is_superuser=superuser)
            s.add(user)
            await s.flush()
            uid = int(user.id)
    return uid, {"Authorization": f"Bearer {create_access_token(uid)}"}


async def _seed_config(**kv):
    async with async_session_maker() as s:
        async with s.begin():
            for key, value in kv.items():
                row = (await s.execute(
                    select(SiteConfig).where(SiteConfig.key == key))).scalars().first()
                if row is None:
                    s.add(SiteConfig(key=key, value=str(value), value_type="string"))
                else:
                    row.value = str(value)
    site_config.clear_cache()


async def _create_pair(client, headers, **overrides):
    response = await client.post("/api/v1/admin/fx/pairs",
                                 json={**PAIR, **overrides}, headers=headers)
    assert response.status_code == 200, response.text
    return int(response.json()["id"])


async def _patch_pair(client, headers, pair_id, **values):
    return await client.patch(f"/api/v1/admin/fx/pairs/{pair_id}",
                              json=values, headers=headers)


async def _set_gate(client, headers, value="true"):
    return await client.put("/api/v1/admin/site-config/fx_short_enabled",
                            json={"value": value}, headers=headers)


async def _stock(pair_id):
    async with async_session_maker() as s:
        pair = await s.get(FxPair, pair_id)
        treasury = (await s.execute(select(FxTreasury).where(
            FxTreasury.pair_id == pair_id))).scalars().one()
        cash = D((await s.execute(
            select(func.coalesce(func.sum(User.cash), ZERO)))).scalar_one())
        wallet = D((await s.execute(
            select(func.coalesce(func.sum(FxWallet.foreign_amount), ZERO)))).scalar_one())
        locked = D((await s.execute(
            select(func.coalesce(func.sum(FxShortPosition.restricted_gold), ZERO)))).scalar_one())
        principal = D((await s.execute(
            select(func.coalesce(func.sum(FxShortPosition.principal_foreign), ZERO))
            .where(FxShortPosition.pair_id == pair_id))).scalar_one())
        return {
            "pool_gold": D(pair.gold_reserve),
            "pool_foreign": D(pair.foreign_reserve),
            "treasury_gold": D(treasury.gold_balance),
            "treasury_foreign": D(treasury.foreign_balance),
            "cash": cash,
            "wallet_foreign": wallet,
            "locked": locked,
            "principal": principal,
            "pool_version": int(pair.pool_version),
        }


async def _open(client, headers, pair_id, *, amount="100", min_out="0", key):
    return await client.post(
        f"/api/v1/fx/pairs/{pair_id}/short/open",
        json={"foreign_amount": amount, "min_gold_out": min_out,
              "idempotency_key": key},
        headers=headers,
    )


async def _cover(client, headers, pair_id, *, amount=None, cover_all=False,
                 max_gold_in="1000000", key):
    body = {"max_gold_in": max_gold_in, "idempotency_key": key}
    if amount is not None:
        body["foreign_amount"] = amount
    if cover_all:
        body["cover_all"] = True
    return await client.post(
        f"/api/v1/fx/pairs/{pair_id}/short/cover", json=body, headers=headers)


async def _short_get(client, headers, pair_id):
    return await client.get(f"/api/v1/fx/pairs/{pair_id}/short", headers=headers)


async def _quote(client, headers, pair_id, payload):
    return await client.post(
        f"/api/v1/fx/pairs/{pair_id}/short/quote", json=payload, headers=headers)


async def _seed_short_direct(pair_id, user_id, *, principal="0", interest="0",
                             restricted="0", proceeds=None, accrued=None):
    """Seed a persisted short row without going through the open route."""
    async with async_session_maker() as s:
        async with s.begin():
            s.add(FxShortPosition(
                user_id=user_id, pair_id=pair_id,
                principal_foreign=D(principal), interest_foreign=D(interest),
                interest_last_accrued_at=accrued,
                restricted_gold=D(restricted),
                proceeds_basis_gold=D(restricted if proceeds is None else proceeds),
            ))


async def _seed_overflow_short(pair_id, user_id, *, principal="999999999999999000"):
    async with async_session_maker() as s:
        async with s.begin():
            s.add(FxShortPosition(
                user_id=user_id, pair_id=pair_id,
                principal_foreign=D(principal), interest_foreign=ZERO,
                interest_last_accrued_at=datetime.now(timezone.utc) - timedelta(days=400),
                restricted_gold=ZERO, proceeds_basis_gold=ZERO,
            ))


async def _seed_wallet(pair_id, user_id, foreign_amount):
    """Seed another pair's positive spot holding without a real spot buy."""
    async with async_session_maker() as s:
        async with s.begin():
            s.add(FxWallet(
                user_id=user_id, pair_id=pair_id,
                foreign_amount=D(foreign_amount), cost_basis=D("0"),
            ))


# ── scenario 1: gate off → explicit admin gate+limit → open+cover ────────────

async def test_gate_off_blocks_open_then_enabled_open_and_cover_conserve_stock(client):
    _, admin_headers = await _make_user(superuser=True)
    user_id, user_headers = await _make_user(cash="1000")
    await _seed_config(fx_enabled="true", loan_enabled="true", loan_daily_rate="0")
    pair_id = await _create_pair(client, admin_headers)

    blocked = await _open(client, user_headers, pair_id, key="k-gate-off")
    assert blocked.status_code == 403, blocked.text
    async with async_session_maker() as s:
        assert (await s.execute(select(FxTrade))).scalars().all() == []
        assert (await s.execute(select(FxShortPosition))).scalars().all() == []

    assert (await _set_gate(client, admin_headers, "true")).status_code == 200
    patched = await _patch_pair(client, admin_headers, pair_id,
                                short_lending_limit_foreign="1000000")
    assert patched.status_code == 200, patched.text
    assert D(patched.json()["short_lending_limit_foreign"]) == D("1000000")

    before = await _stock(pair_id)
    opened = await _open(client, user_headers, pair_id, amount="100", key="k-open")
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert body["purpose"] == "short_open"
    assert body["side"] == "sell"
    assert body["replay"] is False
    assert isinstance(body["output_amount"], str)
    proceeds = D(body["output_amount"])

    after_open = await _stock(pair_id)
    # Borrowed foreign leaves real treasury stock and joins the pool; no wallet.
    assert after_open["treasury_foreign"] == before["treasury_foreign"] - D("100")
    assert after_open["pool_foreign"] == before["pool_foreign"] + D("100")
    assert after_open["wallet_foreign"] == ZERO
    assert after_open["principal"] == D("100")
    assert after_open["cash"] == before["cash"] + proceeds
    assert after_open["locked"] == proceeds
    assert (after_open["pool_gold"] + after_open["treasury_gold"] + after_open["cash"]
            == before["pool_gold"] + before["treasury_gold"] + before["cash"])

    covered = await _cover(client, user_headers, pair_id, cover_all=True,
                           max_gold_in="1000000", key="k-cover")
    assert covered.status_code == 200, covered.text
    cover_body = covered.json()
    assert cover_body["purpose"] == "short_cover"
    assert cover_body["side"] == "buy"

    after_cover = await _stock(pair_id)
    assert after_cover["principal"] == ZERO
    assert after_cover["locked"] == ZERO
    assert after_cover["wallet_foreign"] == ZERO
    # Each currency's real stock is preserved by the full round trip.
    assert (after_cover["pool_gold"] + after_cover["treasury_gold"] + after_cover["cash"]
            == before["pool_gold"] + before["treasury_gold"] + before["cash"])
    assert (after_cover["pool_foreign"] + after_cover["treasury_foreign"]
            + after_cover["wallet_foreign"]
            == before["pool_foreign"] + before["treasury_foreign"]
            + before["wallet_foreign"])
    assert not GATES.held_keys()


# ── scenario 2: idempotent replay and cross-purpose conflicts ────────────────

async def test_same_key_replays_once_and_cross_purpose_conflicts_both_ways(client):
    _, admin_headers = await _make_user(superuser=True)
    user_id, user_headers = await _make_user(cash="100000")
    await _seed_config(fx_enabled="true", loan_enabled="true", loan_daily_rate="0")
    # Two pairs so the spot-first probe does not leave a wallet that blocks the
    # later same-pair short; the conflict is keyed per user, not per pair.
    spot_pair = await _create_pair(client, admin_headers, currency_code="EUR",
                                   short_lending_limit_foreign="1000000")
    pair_id = await _create_pair(client, admin_headers,
                                 short_lending_limit_foreign="1000000")
    assert (await _set_gate(client, admin_headers, "true")).status_code == 200

    # A spot trade first, then a short open on the same key: conflict.
    spot_key = "k-cross-spot-first"
    spot = await client.post(
        f"/api/v1/fx/pairs/{spot_pair}/trades",
        json={"side": "buy", "amount": "10", "min_out": "0",
              "idempotency_key": spot_key},
        headers=user_headers,
    )
    assert spot.status_code == 200, spot.text
    cross = await _open(client, user_headers, pair_id, amount="10", key=spot_key)
    assert cross.status_code == 409, cross.text

    opened = await _open(client, user_headers, pair_id, amount="100", key="k-open")
    assert opened.status_code == 200, opened.text
    treasury_after_open = (await _stock(pair_id))["treasury_foreign"]

    replay = await _open(client, user_headers, pair_id, amount="100", key="k-open")
    assert replay.status_code == 200, replay.text
    assert replay.json()["replay"] is True
    assert replay.json()["trade_id"] == opened.json()["trade_id"]
    assert (await _stock(pair_id))["treasury_foreign"] == treasury_after_open

    changed = await _open(client, user_headers, pair_id, amount="101", key="k-open")
    assert changed.status_code == 409, changed.text

    # A short cover first, then a spot buy that matches pair/side/amount/min_out:
    # without a purpose check this would replay the cover as an ordinary buy.
    cover = await _cover(client, user_headers, pair_id, amount="50",
                         max_gold_in="1000000", key="k-cross-short-first")
    assert cover.status_code == 200, cover.text
    cover_input = cover.json()["input_amount"]
    book_before = await _stock(pair_id)
    reverse = await client.post(
        f"/api/v1/fx/pairs/{pair_id}/trades",
        json={"side": "buy", "amount": cover_input, "min_out": "0",
              "idempotency_key": "k-cross-short-first"},
        headers=user_headers,
    )
    assert reverse.status_code == 409, reverse.text
    book_after = await _stock(pair_id)
    assert book_before == book_after
    async with async_session_maker() as s:
        purposes = sorted(t.purpose for t in (await s.execute(
            select(FxTrade).where(FxTrade.user_id == user_id))).scalars().all())
    # One spot buy, one short open, one short cover: no replay trade appeared.
    assert purposes.count("short_cover") == 1
    assert purposes.count("short_open") == 1
    assert not GATES.held_keys()


# ── scenario 3: reduce_only blocks ordinary buy/open but allows cover ────────

@pytest.mark.parametrize("status", ["trading", "paused"])
async def test_reduce_only_blocks_buy_and_open_but_allows_cover(client, status):
    _, admin_headers = await _make_user(superuser=True)
    _, user_headers = await _make_user(cash="100000")
    await _seed_config(fx_enabled="true", loan_enabled="true", loan_daily_rate="0")
    pair_id = await _create_pair(client, admin_headers,
                                 short_lending_limit_foreign="1000000")
    assert (await _set_gate(client, admin_headers, "true")).status_code == 200
    opened = await _open(client, user_headers, pair_id, amount="100", key="k-open")
    assert opened.status_code == 200, opened.text

    patched = await _patch_pair(client, admin_headers, pair_id,
                                reduce_only=True, status=status)
    assert patched.status_code == 200, patched.text

    buy = await client.post(
        f"/api/v1/fx/pairs/{pair_id}/trades",
        json={"side": "buy", "amount": "1", "min_out": "0", "idempotency_key": "k-buy"},
        headers=user_headers,
    )
    assert buy.status_code == 403, buy.text
    opened_again = await _open(client, user_headers, pair_id, amount="1", key="k-open-2")
    assert opened_again.status_code == 403, opened_again.text

    covered = await _cover(client, user_headers, pair_id, cover_all=True,
                           max_gold_in="1000000", key="k-cover-ro")
    assert covered.status_code == 200, covered.text
    assert (await _stock(pair_id))["principal"] == ZERO
    assert not GATES.held_keys()


# ── scenario 3b: opening/loan gate off never strands a live short ────────────

async def test_opening_and_loan_gates_off_still_allow_cover(client):
    _, admin_headers = await _make_user(superuser=True)
    _, user_headers = await _make_user(cash="100000")
    await _seed_config(fx_enabled="true", loan_enabled="true", loan_daily_rate="0")
    pair_id = await _create_pair(client, admin_headers,
                                 short_lending_limit_foreign="1000000")
    assert (await _set_gate(client, admin_headers, "true")).status_code == 200
    opened = await _open(client, user_headers, pair_id, amount="100", key="k-open")
    assert opened.status_code == 200, opened.text

    # Turn the site opening gate and the loan gate off; cover must still work.
    assert (await _set_gate(client, admin_headers, "false")).status_code == 200
    await _seed_config(loan_enabled="false")
    new_open = await _open(client, user_headers, pair_id, amount="1", key="k-open-2")
    assert new_open.status_code == 403, new_open.text

    covered = await _cover(client, user_headers, pair_id, cover_all=True,
                           max_gold_in="1000000", key="k-cover-off")
    assert covered.status_code == 200, covered.text
    assert (await _stock(pair_id))["principal"] == ZERO
    assert not GATES.held_keys()


# ── scenario 4: malformed shape and debt overflow are 4xx, never 500 ─────────

@pytest.mark.parametrize("payload", [
    {"foreign_amount": "NaN", "min_gold_out": "0"},
    {"foreign_amount": "0", "min_gold_out": "0"},
    {"foreign_amount": "-1", "min_gold_out": "0"},
    {"foreign_amount": "1.0000001", "min_gold_out": "0"},
    {"foreign_amount": "1", "min_gold_out": "1.0000001"},
    {"foreign_amount": "1", "min_gold_out": "10000000000"},
    {"foreign_amount": "1", "min_gold_out": "NaN"},
    {"foreign_amount": "1", "min_gold_out": "-1"},
    {"foreign_amount": "1", "min_gold_out": "0", "idempotency_key": ""},
    {"foreign_amount": "1", "min_gold_out": "0",
     "idempotency_key": "x" * 129},
])
async def test_malformed_open_requests_are_422_without_writes(client, payload):
    _, admin_headers = await _make_user(superuser=True)
    _, user_headers = await _make_user(cash="1000")
    await _seed_config(fx_enabled="true", loan_enabled="true", loan_daily_rate="0")
    pair_id = await _create_pair(client, admin_headers,
                                 short_lending_limit_foreign="1000000")
    assert (await _set_gate(client, admin_headers, "true")).status_code == 200

    response = await client.post(
        f"/api/v1/fx/pairs/{pair_id}/short/open",
        json={**payload, "idempotency_key": payload.get("idempotency_key", "k-bad")},
        headers=user_headers,
    )
    assert response.status_code == 422, response.text
    async with async_session_maker() as s:
        assert (await s.execute(select(FxTrade))).scalars().all() == []
        assert (await s.execute(select(FxShortPosition))).scalars().all() == []


@pytest.mark.parametrize("payload", [
    {"foreign_amount": "1", "cover_all": True, "max_gold_in": "0"},
    {"max_gold_in": "0"},
    {"cover_all": True, "max_gold_in": "NaN"},
    {"cover_all": True, "max_gold_in": "-1"},
    {"cover_all": True, "max_gold_in": "10000000000"},
    {"cover_all": True, "max_gold_in": "0.0000001"},
    {"foreign_amount": "0", "max_gold_in": "0"},
    {"cover_all": True, "max_gold_in": "0", "idempotency_key": ""},
])
async def test_malformed_cover_requests_are_422_without_writes(client, payload):
    _, admin_headers = await _make_user(superuser=True)
    _, user_headers = await _make_user(cash="1000")
    await _seed_config(fx_enabled="true", loan_enabled="true", loan_daily_rate="0")
    pair_id = await _create_pair(client, admin_headers,
                                 short_lending_limit_foreign="1000000")
    assert (await _set_gate(client, admin_headers, "true")).status_code == 200

    response = await client.post(
        f"/api/v1/fx/pairs/{pair_id}/short/cover",
        json={**payload, "idempotency_key": payload.get("idempotency_key", "k-bad")},
        headers=user_headers,
    )
    assert response.status_code == 422, response.text
    async with async_session_maker() as s:
        assert (await s.execute(select(FxTrade))).scalars().all() == []


async def test_pending_debt_overflow_is_4xx_not_500_and_changes_nothing(client):
    _, admin_headers = await _make_user(superuser=True)
    user_id, user_headers = await _make_user(cash="1000")
    await _seed_config(fx_enabled="true", loan_enabled="true", loan_daily_rate="0.5")
    pair_id = await _create_pair(client, admin_headers,
                                 short_lending_limit_foreign="999999999999999999.999999")
    assert (await _set_gate(client, admin_headers, "true")).status_code == 200
    await _seed_overflow_short(pair_id, user_id)

    async with async_session_maker() as s:
        before = sorted(
            (int(t.id), t.purpose) for t in (await s.execute(select(FxTrade))).scalars().all())

    response = await _open(client, user_headers, pair_id, amount="1", key="k-overflow")
    assert 400 <= response.status_code < 500, response.text
    assert response.status_code == 422, response.text

    async with async_session_maker() as s:
        after = sorted(
            (int(t.id), t.purpose) for t in (await s.execute(select(FxTrade))).scalars().all())
    assert after == before


async def test_unknown_pair_is_404_not_500_for_short_writes(client):
    _, admin_headers = await _make_user(superuser=True)
    _, user_headers = await _make_user(cash="1000")
    await _seed_config(fx_enabled="true", loan_enabled="true", loan_daily_rate="0")
    assert (await _set_gate(client, admin_headers, "true")).status_code == 200

    opened = await _open(client, user_headers, 999999, amount="1", key="k-404")
    assert opened.status_code == 404, opened.text
    covered = await _cover(client, user_headers, 999999, cover_all=True, key="k-404c")
    assert covered.status_code == 404, covered.text
    assert not GATES.held_keys()


# ── scenario 5: admin pair cap surface and public boundary ──────────────────

async def test_pair_limit_validation_admin_read_audit_and_public_boundary(client):
    _, admin_headers = await _make_user(superuser=True)
    for bad in ("-1", "1.0000001", "NaN", "Infinity",
                "1000000000000000000"):
        response = await client.post(
            "/api/v1/admin/fx/pairs",
            json={**PAIR, "currency_code": "BAD", "short_lending_limit_foreign": bad},
            headers=admin_headers,
        )
        assert response.status_code == 422, (bad, response.text)

    pair_id = await _create_pair(client, admin_headers,
                                 short_lending_limit_foreign="12.5")
    listing = await client.get("/api/v1/admin/fx/pairs", headers=admin_headers)
    assert listing.status_code == 200, listing.text
    row = next(r for r in listing.json() if r["id"] == pair_id)
    assert D(row["short_lending_limit_foreign"]) == D("12.5")

    public = await client.get("/api/v1/fx/pairs")
    assert public.status_code == 200
    assert "short_lending_limit_foreign" not in public.text
    assert "short_lending_limit_foreign" not in (await client.get(
        f"/api/v1/fx/pairs/{pair_id}/snapshot")).text

    bumped = (await _stock(pair_id))["pool_version"]
    patched = await _patch_pair(client, admin_headers, pair_id,
                                short_lending_limit_foreign="0")
    assert patched.status_code == 200, patched.text
    assert D(patched.json()["short_lending_limit_foreign"]) == ZERO
    assert (await _stock(pair_id))["pool_version"] > bumped

    rejected = await _patch_pair(client, admin_headers, pair_id,
                                 short_lending_limit_foreign="-5")
    assert rejected.status_code == 422, rejected.text

    async with async_session_maker() as s:
        audit = (await s.execute(select(AuditEvent).where(
            AuditEvent.event_type == "fx_pair_update",
            AuditEvent.ref_id == pair_id,
        ).order_by(AuditEvent.id.desc()))).scalars().first()
    assert audit is not None
    assert audit.payload["after"]["short_lending_limit_foreign"] == "0"


# ── scenario 5b: admin gate toggle + old spot response contract ─────────────

async def test_admin_short_gate_toggle_and_spot_contract_unchanged(client):
    _, admin_headers = await _make_user(superuser=True)
    _, user_headers = await _make_user(cash="100000")
    await _seed_config(fx_enabled="true", loan_enabled="true", loan_daily_rate="0")
    pair_id = await _create_pair(client, admin_headers)

    assert (await _set_gate(client, admin_headers, "maybe")).status_code == 400
    assert (await _set_gate(client, admin_headers, "false")).status_code == 200
    assert (await _set_gate(client, admin_headers, "true")).status_code == 200
    config = await client.get("/api/v1/admin/site-config", headers=admin_headers)
    assert any(item["key"] == "fx_short_enabled" and item["value"] == "true"
               for item in config.json())

    spot = await client.post(
        f"/api/v1/fx/pairs/{pair_id}/trades",
        json={"side": "buy", "amount": "1", "min_out": "0",
              "idempotency_key": "k-spot-contract"},
        headers=user_headers,
    )
    assert spot.status_code == 200, spot.text
    assert set(spot.json().keys()) == SPOT_TRADE_KEYS

    pairs = await client.get("/api/v1/fx/pairs")
    assert set(pairs.json()[0].keys()) == {
        "id", "currency_code", "currency_name", "status", "reduce_only",
        "pool_version", "created_at", "updated_at",
    }


# ── scenario 6: cross-pair same-key unique race maps to 409, never 500 ───────

async def _commit_winner_short_open(*, user_id, pair_id, key, foreign="100"):
    """Commit the row a concurrent winner request would have produced.

    SQLite gives no deterministic cross-connection interleaving, so the loser
    request's post-lookup window is opened by committing this real ``FxTrade``
    from a second session while the loser already passed its idempotency lookup.
    The unique ``uq_fx_trade_user_idempotency`` violation the loser then hits at
    flush is the genuine database error; only its timing is arranged.
    """
    async with async_session_maker() as s:
        async with s.begin():
            trade = FxTrade(
                pair_id=pair_id, user_id=user_id, side="sell", purpose="short_open",
                requested_foreign_amount=D(foreign), input_amount=D(foreign),
                output_amount=D("99"), min_out=ZERO, fee_amount=ZERO,
                pre_gold_reserve=D("1000"), pre_foreign_reserve=D("1000"),
                post_gold_reserve=D("1901"), post_foreign_reserve=D("900"),
                post_price=D("1"), source="player", idempotency_key=key,
            )
            s.add(trade)
            await s.flush()
            return int(trade.id)


async def test_cross_pair_same_key_race_maps_unique_violation_to_409(
        client, monkeypatch):
    _, admin_headers = await _make_user(superuser=True)
    user_id, user_headers = await _make_user(cash="1000")
    await _seed_config(fx_enabled="true", loan_enabled="true", loan_daily_rate="0")
    loser_pair = await _create_pair(client, admin_headers, currency_code="AAA",
                                    short_lending_limit_foreign="1000000")
    winner_pair = await _create_pair(client, admin_headers, currency_code="BBB")
    assert (await _set_gate(client, admin_headers, "true")).status_code == 200

    key = "k-concurrent-cross-pair"
    published: list[tuple] = []
    monkeypatch.setattr(publisher, "enqueue_publication",
                        lambda *a, **k: published.append((a, k)))

    # The winner appears only after the loser has locked/read and missed its
    # idempotency lookup, so the loser cannot replay it and reaches flush.
    real_get_bool_or = site_config.get_bool_or
    injected = {"winner_id": None}

    async def _seam(session, config_key, default):
        if injected["winner_id"] is None:
            injected["winner_id"] = await _commit_winner_short_open(
                user_id=user_id, pair_id=winner_pair, key=key)
        return await real_get_bool_or(session, config_key, default)

    monkeypatch.setattr(site_config, "get_bool_or", _seam)

    before = await _stock(loser_pair)
    async with async_session_maker() as s:
        audit_before = int((await s.execute(
            select(func.count()).select_from(AuditEvent)
            .where(AuditEvent.user_id == user_id))).scalar_one())

    loser = await _open(client, user_headers, loser_pair, amount="100", key=key)
    assert loser.status_code == 409, loser.text
    winner_id = injected["winner_id"]
    assert winner_id is not None

    # The losing request rolled back every pending money/lock/audit mutation.
    assert await _stock(loser_pair) == before
    async with async_session_maker() as s:
        trades = (await s.execute(select(FxTrade)
                                  .where(FxTrade.user_id == user_id))).scalars().all()
        shorts = (await s.execute(select(FxShortPosition)
                                  .where(FxShortPosition.user_id == user_id))).scalars().all()
        audit_after = int((await s.execute(
            select(func.count()).select_from(AuditEvent)
            .where(AuditEvent.user_id == user_id))).scalar_one())
    assert [int(t.id) for t in trades] == [winner_id]
    assert shorts == []
    assert audit_after == audit_before
    assert published == [] and not GATES.held_keys()

    # The winner's row is the saved trade a same-key retry can retrieve; it
    # replays without a second borrow.
    replay = await _open(client, user_headers, winner_pair, amount="100", key=key)
    assert replay.status_code == 200, replay.text
    assert replay.json()["replay"] is True
    assert replay.json()["trade_id"] == winner_id
    assert published == []


async def test_short_wrapper_reraises_unrelated_integrity_error(client):
    """A non-idempotency DB integrity failure must stay a server error."""
    _, admin_headers = await _make_user(superuser=True)
    user_id, _ = await _make_user(cash="1000")
    await _seed_config(fx_enabled="true", loan_enabled="true", loan_daily_rate="0")
    pair_id = await _create_pair(client, admin_headers,
                                 short_lending_limit_foreign="1000000")

    async def run_in_session(db, deps):
        # Real unique violation from an unrelated table (site_config.key).
        db.add(SiteConfig(key="dup-key", value="a", value_type="string"))
        await db.flush()
        db.add(SiteConfig(key="dup-key", value="b", value_type="string"))
        await db.flush()

    async with async_session_maker() as s:
        with pytest.raises(IntegrityError):
            await shorts._execute_player_short_write(
                s, user_id=user_id, pair_id=pair_id, run_in_session=run_in_session)


# ══════════════════════ Task 3c2: short read and indicative quote ═════════════

async def test_short_read_is_own_position_and_reference_cover_without_clock_advance(client):
    """A user only ever sees their own pending foreign debt and reference K.

    Regression: reading another account's obligation, leaking treasury stock or
    the pair lending cap, using a stored/zero reference cost, or advancing the
    persisted interest clock as a side effect of a GET.
    """
    _, admin_headers = await _make_user(superuser=True)
    user_id, user_headers = await _make_user(cash="1000")
    other_id, other_headers = await _make_user(cash="1000")
    fresh_id, fresh_headers = await _make_user(cash="10")
    await _seed_config(fx_enabled="true", loan_enabled="true", loan_daily_rate="0.01")
    pair_id = await _create_pair(client, admin_headers, gold_reserve="1000",
                                 foreign_reserve="1000", buy_fee_rate="0.02",
                                 sell_fee_rate="0.01",
                                 short_lending_limit_foreign="1000000")
    assert (await _set_gate(client, admin_headers, "true")).status_code == 200
    assert (await _open(client, user_headers, pair_id, amount="100",
                        key="k-read-own")).status_code == 200
    assert (await _open(client, other_headers, pair_id, amount="40",
                        key="k-read-other")).status_code == 200

    # No row for this pair: zeroed position, not a 404, and no treasury leak.
    empty = await _short_get(client, fresh_headers, pair_id)
    assert empty.status_code == 200, empty.text
    empty_body = empty.json()
    assert D(empty_body["principal_foreign"]) == ZERO
    assert D(empty_body["pending_short_debt"]) == ZERO
    assert D(empty_body["reference_cover_cost"]) == ZERO
    assert empty_body["risk_status"] == "ok"

    async with async_session_maker() as s:
        own = (await s.execute(select(FxShortPosition).where(
            FxShortPosition.user_id == user_id,
            FxShortPosition.pair_id == pair_id))).scalars().one()
        principal_before = D(own.principal_foreign)
        interest_before = D(own.interest_foreign)
        accrued_before = own.interest_last_accrued_at
        pair_row = await s.get(FxPair, pair_id)

    response = await _short_get(client, user_headers, pair_id)
    assert response.status_code == 200, response.text
    body = response.json()
    # Only this user's obligation (not the other user's 40) is present.
    assert D(body["principal_foreign"]) == principal_before
    assert isinstance(body["principal_foreign"], str)
    assert isinstance(body["pending_short_debt"], str)
    assert isinstance(body["restricted_gold"], str)
    assert isinstance(body["reference_cover_cost"], str)
    assert D(body["interest_foreign"]) == interest_before
    assert D(body["pending_short_debt"]) >= principal_before
    locked = D(body["restricted_gold"])
    assert locked > ZERO
    assert D(body["proceeds_basis_gold"]) == locked
    assert D(body["reference_cover_cost"]) > ZERO
    assert body["executable"] is True
    assert body["risk_status"] == "ok"
    assert body["blocked_reason"] is None

    # Hidden operator/system fields never leak through the player read.
    for hidden in ("foreign_balance", "gold_balance",
                   "short_lending_limit_foreign", "pool_version"):
        assert hidden not in body
    # The reference K quotes exactly this user's pending debt at the live pool.
    k = quote_buy_exact_out(
        D(body["pending_short_debt"]), D(pair_row.gold_reserve),
        D(pair_row.foreign_reserve), D(pair_row.buy_fee_rate)).input_amount
    assert D(body["reference_cover_cost"]) == k

    # GET never advances or writes the stored debt clock.
    async with async_session_maker() as s:
        after = (await s.execute(select(FxShortPosition).where(
            FxShortPosition.user_id == user_id,
            FxShortPosition.pair_id == pair_id))).scalars().one()
    assert D(after.interest_foreign) == interest_before
    assert after.interest_last_accrued_at == accrued_before
    assert D(after.principal_foreign) == principal_before


async def test_open_quote_matches_amm_and_execution_requotes_at_current_pool(client):
    """An open quote is real ``quote_sell`` math and execution re-quotes.

    Regression: quoting total cash instead of the net AMM sell price, returning
    a stale price at execution time, or a quote that secretly reserves the pool.
    """
    _, admin_headers = await _make_user(superuser=True)
    user_id, user_headers = await _make_user(cash="1000")
    await _seed_config(fx_enabled="true", loan_enabled="true", loan_daily_rate="0")
    pair_id = await _create_pair(client, admin_headers, gold_reserve="1000",
                                 foreign_reserve="1000", buy_fee_rate="0.02",
                                 sell_fee_rate="0.01",
                                 short_lending_limit_foreign="1000000")
    assert (await _set_gate(client, admin_headers, "true")).status_code == 200

    before = await _stock(pair_id)
    quote = await _quote(client, user_headers, pair_id,
                         {"action": "open", "foreign_amount": "100"})
    assert quote.status_code == 200, quote.text
    body = quote.json()
    assert body["action"] == "open"
    assert body["purpose"] == "short_open"
    assert body["executable"] is True
    assert D(body["requested_foreign_amount"]) == D("100")
    assert D(body["actual_foreign_amount"]) == D("100")
    assert body["fee_currency"] == "foreign"
    # New money/quantity fields stay decimal strings, never JSON numbers.
    for field in ("input_amount", "output_amount", "fee_amount",
                  "restricted_gold_delta", "estimated_equity"):
        assert isinstance(body[field], str), field
    expected_sell = quote_sell(D("100"), D("1000"), D("1000"), D("0.01"))
    assert D(body["input_amount"]) == expected_sell.input_amount
    assert D(body["output_amount"]) == expected_sell.output_amount
    assert D(body["fee_amount"]) == expected_sell.fee_amount
    assert D(body["post_price"]) == expected_sell.post_price.quantize(Q6)
    assert D(body["restricted_gold_delta"]) == expected_sell.output_amount
    assert body["estimated_equity"] is not None
    assert body["estimated_risk_basis"] is not None
    assert body["pool_version"] == before["pool_version"]
    assert body["expires_at"] is not None
    # Advisory: the quote neither reserves the pool nor writes a trade/short.
    assert await _stock(pair_id) == before

    opened = await _open(client, user_headers, pair_id, amount="100",
                         min_out="0", key="k-open-quote")
    assert opened.status_code == 200, opened.text
    assert D(opened.json()["output_amount"]) == D(body["output_amount"])
    assert D(opened.json()["fee_amount"]) == D(body["fee_amount"])
    assert D(opened.json()["post_price"]) == D(body["post_price"])

    # The quote's simulated post-order risk equals the persisted post-open
    # valuation: same W/K math, no second model.
    async with async_session_maker() as s:
        actual = await value_user_detailed(s, user_id, daily_rate=D("0"))
    assert actual.liquidation_equity == D(body["estimated_equity"])
    assert actual.risk_basis == D(body["estimated_risk_basis"])

    # Move the pool with another account's spot buy; the next open order must
    # re-quote against the moved pool rather than reuse the earlier quote.
    _, mover_headers = await _make_user(cash="1000")
    moved_response = await client.post(
        f"/api/v1/fx/pairs/{pair_id}/trades",
        json={"side": "buy", "amount": "200", "min_out": "0",
              "idempotency_key": "k-move-pool"},
        headers=mover_headers,
    )
    assert moved_response.status_code == 200, moved_response.text
    moved = await _stock(pair_id)

    quote2 = await _quote(client, user_headers, pair_id,
                          {"action": "open", "foreign_amount": "50"})
    assert quote2.status_code == 200, quote2.text
    body2 = quote2.json()
    expected_moved = quote_sell(D("50"), moved["pool_gold"], moved["pool_foreign"], D("0.01"))
    assert D(body2["output_amount"]) == expected_moved.output_amount
    assert D(body2["output_amount"]) != D(body["output_amount"])

    opened2 = await _open(client, user_headers, pair_id, amount="50",
                          key="k-open-quote-2")
    assert opened2.status_code == 200, opened2.text
    assert D(opened2.json()["output_amount"]) == D(body2["output_amount"])


async def test_cover_all_quote_uses_pending_interest_exact_cost_and_release(client):
    """``cover_all`` prices the settled principal+interest and releases only its lock.

    Regression: quoting the stored principal, an exact-input (slippage-losing)
    buy, or refusing a reduce-only cover because post-trade admission fails.
    """
    _, admin_headers = await _make_user(superuser=True)
    user_id, user_headers = await _make_user(cash="1000")
    await _seed_config(fx_enabled="true", loan_enabled="true", loan_daily_rate="0")
    pair_id = await _create_pair(client, admin_headers, gold_reserve="1000",
                                 foreign_reserve="1000", buy_fee_rate="0.02",
                                 sell_fee_rate="0.01",
                                 short_lending_limit_foreign="1000000")
    assert (await _set_gate(client, admin_headers, "true")).status_code == 200
    assert (await _open(client, user_headers, pair_id, amount="100",
                        key="k-cover-all")).status_code == 200
    async with async_session_maker() as s:
        async with s.begin():
            row = (await s.execute(select(FxShortPosition).where(
                FxShortPosition.user_id == user_id,
                FxShortPosition.pair_id == pair_id))).scalars().one()
            row.interest_foreign = D("1.5")
            row.interest_last_accrued_at = datetime.now(timezone.utc)

    stock = await _stock(pair_id)
    quote = await _quote(client, user_headers, pair_id,
                         {"action": "cover", "cover_all": True})
    assert quote.status_code == 200, quote.text
    body = quote.json()
    assert body["action"] == "cover"
    assert body["purpose"] == "short_cover"
    assert body["cover_all"] is True
    assert body["requested_foreign_amount"] is None
    assert D(body["actual_foreign_amount"]) == D("101.5")
    expected_buy = quote_buy_exact_out(
        D("101.5"), stock["pool_gold"], stock["pool_foreign"], D("0.02"))
    assert D(body["input_amount"]) == expected_buy.input_amount
    assert D(body["output_amount"]) == D("101.5")
    assert D(body["fee_amount"]) == expected_buy.fee_amount
    assert body["fee_currency"] == "gold"
    # A full cover releases exactly this position's own lock.
    assert D(body["restricted_gold_delta"]) == -stock["locked"].quantize(Q6)
    assert body["executable"] is True
    assert body["blocked_reason"] is None
    assert body["estimated_equity"] is not None

    # A cover that leaves the account below the initial-margin admission is
    # still a reduce-only quote: numeric post-state, not a refusal.
    margin_user_id, margin_headers = await _make_user(cash="200")
    async with async_session_maker() as s:
        async with s.begin():
            user = await s.get(User, margin_user_id)
            user.debt = D("1000")
    margin_pair = await _create_pair(client, admin_headers, currency_code="MRO",
                                     gold_reserve="1000", foreign_reserve="1000",
                                     short_lending_limit_foreign="0")
    await _seed_short_direct(margin_pair, margin_user_id, principal="100",
                             restricted="200",
                             accrued=datetime.now(timezone.utc))
    margin_quote = await _quote(client, margin_headers, margin_pair,
                                {"action": "cover", "cover_all": True})
    assert margin_quote.status_code == 200, margin_quote.text
    margin_body = margin_quote.json()
    assert margin_body["executable"] is True
    assert margin_body["blocked_reason"] is None
    assert margin_body["risk_status"] == "ok"
    assert margin_body["estimated_equity"] is not None
    assert D(margin_body["estimated_equity"]) < ZERO


async def test_cover_quote_blocks_pool_exhaustion_but_allows_smaller_and_paused(client):
    """q >= F is a null-cost block; a smaller q still quotes; paused differs.

    Regression: emitting a zero/Infinity cover cost for an unquotable debt,
    blocking every partial cover because the full debt is unquotable, or
    collapsing "known but not executable" into "unknown cost".
    """
    _, admin_headers = await _make_user(superuser=True)
    user_id, user_headers = await _make_user(cash="1000")
    await _seed_config(fx_enabled="true", loan_enabled="true", loan_daily_rate="0")
    pair_id = await _create_pair(client, admin_headers, gold_reserve="1000",
                                 foreign_reserve="50",
                                 short_lending_limit_foreign="0")
    await _seed_short_direct(pair_id, user_id, principal="100", restricted="300",
                             accrued=datetime.now(timezone.utc))

    full = await _quote(client, user_headers, pair_id,
                        {"action": "cover", "cover_all": True})
    assert full.status_code == 200, full.text
    full_body = full.json()
    assert full_body["input_amount"] is None
    assert full_body["output_amount"] is None
    assert full_body["fee_amount"] is None
    assert full_body["risk_status"] == "blocked"
    assert full_body["blocked_reason"] == "insufficient_pool_foreign"
    assert full_body["executable"] is False

    small = await _quote(client, user_headers, pair_id,
                         {"action": "cover", "foreign_amount": "10"})
    assert small.status_code == 200, small.text
    small_body = small.json()
    expected = quote_buy_exact_out(D("10"), D("1000"), D("50"), ZERO)
    assert D(small_body["input_amount"]) == expected.input_amount
    assert D(small_body["output_amount"]) == D("10")
    assert small_body["executable"] is True
    # Remaining 90 >= F leaves the full-portfolio risk incomplete/null.
    assert small_body["risk_status"] == "blocked"
    assert small_body["estimated_equity"] is None
    assert small_body["blocked_reason"] is None
    assert small_body["risk_blocked_reason"] == "insufficient_pool_foreign"

    paused_pair = await _create_pair(client, admin_headers, currency_code="MRO",
                                     status="paused", reduce_only=False,
                                     gold_reserve="1000", foreign_reserve="1000",
                                     short_lending_limit_foreign="0")
    paused_user_id, paused_headers = await _make_user(cash="1000")
    await _seed_short_direct(paused_pair, paused_user_id, principal="100",
                             restricted="300",
                             accrued=datetime.now(timezone.utc))
    paused = await _quote(client, paused_headers, paused_pair,
                          {"action": "cover", "cover_all": True})
    assert paused.status_code == 200, paused.text
    paused_body = paused.json()
    # Finite math K is known; only execution is unavailable.
    assert paused_body["input_amount"] is not None
    assert paused_body["risk_status"] == "ok"
    assert paused_body["executable"] is False
    assert paused_body["blocked_reason"] == "pair_not_coverable"
    assert paused_body["estimated_equity"] is not None

    # The read model keeps "known but not executable" distinct from "unknown".
    paused_read = (await _short_get(client, paused_headers, paused_pair)).json()
    assert paused_read["reference_cover_cost"] is not None
    assert paused_read["risk_status"] == "ok"
    assert paused_read["executable"] is False
    assert paused_read["blocked_reason"] == "pair_paused"

    unknown_read = (await _short_get(client, user_headers, pair_id)).json()
    assert unknown_read["reference_cover_cost"] is None
    assert unknown_read["risk_status"] == "blocked"
    assert unknown_read["blocked_reason"] == "insufficient_pool_foreign"
    assert unknown_read["executable"] is False


@pytest.mark.parametrize("payload", [
    {"action": "open"},
    {"action": "open", "foreign_amount": "NaN"},
    {"action": "open", "foreign_amount": "0"},
    {"action": "open", "foreign_amount": "-1"},
    {"action": "open", "foreign_amount": "1.0000001"},
    {"action": "open", "foreign_amount": "1", "cover_all": True},
    {"action": "cover"},
    {"action": "cover", "foreign_amount": "1", "cover_all": True},
    {"action": "cover", "foreign_amount": "0"},
    {"action": "cover", "foreign_amount": "1.0000001"},
    {"action": "bogus", "foreign_amount": "1"},
])
async def test_invalid_quote_shape_is_422_without_writes(client, payload):
    """NaN / overprecision / wrong action shape never reaches the ledger."""
    _, admin_headers = await _make_user(superuser=True)
    _, user_headers = await _make_user(cash="1000")
    await _seed_config(fx_enabled="true", loan_enabled="true", loan_daily_rate="0")
    pair_id = await _create_pair(client, admin_headers,
                                 short_lending_limit_foreign="1000000")
    assert (await _set_gate(client, admin_headers, "true")).status_code == 200

    response = await _quote(client, user_headers, pair_id, payload)
    assert response.status_code == 422, (payload, response.text)
    async with async_session_maker() as s:
        assert (await s.execute(select(FxTrade))).scalars().all() == []
        assert (await s.execute(select(FxShortPosition))).scalars().all() == []


async def test_cover_quote_without_short_is_blocked_without_writes(client):
    """A cover quote with no short never becomes a spot buy or a wallet."""
    _, admin_headers = await _make_user(superuser=True)
    user_id, user_headers = await _make_user(cash="1000")
    await _seed_config(fx_enabled="true", loan_enabled="true", loan_daily_rate="0")
    pair_id = await _create_pair(client, admin_headers,
                                 short_lending_limit_foreign="1000000")
    assert (await _set_gate(client, admin_headers, "true")).status_code == 200

    for payload in ({"action": "cover", "cover_all": True},
                    {"action": "cover", "foreign_amount": "1"}):
        response = await _quote(client, user_headers, pair_id, payload)
        assert response.status_code == 200, (payload, response.text)
        body = response.json()
        assert body["executable"] is False
        assert body["blocked_reason"] == "no_outstanding_short"
        assert body["input_amount"] is None
        assert body["actual_foreign_amount"] is None

    async with async_session_maker() as s:
        assert (await s.execute(select(FxTrade))).scalars().all() == []
        assert (await s.execute(select(FxShortPosition))).scalars().all() == []
        wallet = (await s.execute(select(FxWallet).where(
            FxWallet.user_id == user_id))).scalars().all()
        assert wallet == []


async def test_quote_status_matrix_gates_reduce_only_and_closed(client):
    """The quote status matrix matches the write-route execution matrix.

    Regression: a closed/short-gate-off pair still advertising an executable
    open, a cover being suppressed with opening debt on the books, or
    ``fx_enabled=false`` failing to stop player quote execution.
    """
    _, admin_headers = await _make_user(superuser=True)
    _, user_headers = await _make_user(cash="100000")
    await _seed_config(fx_enabled="true", loan_enabled="true", loan_daily_rate="0")
    pair_id = await _create_pair(client, admin_headers,
                                 short_lending_limit_foreign="1000000")
    assert (await _set_gate(client, admin_headers, "true")).status_code == 200
    assert (await _open(client, user_headers, pair_id, amount="100",
                        key="k-matrix-open")).status_code == 200

    # Opening gate off must not suppress a cover for the live debt.
    assert (await _set_gate(client, admin_headers, "false")).status_code == 200
    open_quote = await _quote(client, user_headers, pair_id,
                              {"action": "open", "foreign_amount": "10"})
    assert open_quote.status_code == 200, open_quote.text
    assert open_quote.json()["executable"] is False
    assert open_quote.json()["blocked_reason"] == "short_disabled"
    cover_quote = await _quote(client, user_headers, pair_id,
                               {"action": "cover", "cover_all": True})
    assert cover_quote.status_code == 200, cover_quote.text
    assert cover_quote.json()["executable"] is True

    # The pair lending cap of zero blocks a new open quote but not the cover.
    assert (await _set_gate(client, admin_headers, "true")).status_code == 200
    assert (await _patch_pair(client, admin_headers, pair_id,
                              short_lending_limit_foreign="0")).status_code == 200
    capped = await _quote(client, user_headers, pair_id,
                          {"action": "open", "foreign_amount": "10"})
    assert capped.status_code == 200, capped.text
    assert capped.json()["executable"] is False
    assert capped.json()["blocked_reason"] == "short_lending_limit"
    still_cover = await _quote(client, user_headers, pair_id,
                               {"action": "cover", "foreign_amount": "10"})
    assert still_cover.status_code == 200, still_cover.text
    assert still_cover.json()["executable"] is True

    # paused + reduce_only allows cover; paused full-stop / closed do not.
    assert (await _patch_pair(client, admin_headers, pair_id, status="paused",
                              reduce_only=True)).status_code == 200
    paused_ro = await _quote(client, user_headers, pair_id,
                             {"action": "cover", "cover_all": True})
    assert paused_ro.status_code == 200, paused_ro.text
    assert paused_ro.json()["executable"] is True

    assert (await _patch_pair(client, admin_headers, pair_id, status="paused",
                              reduce_only=False)).status_code == 200
    paused = await _quote(client, user_headers, pair_id,
                          {"action": "cover", "cover_all": True})
    assert paused.status_code == 200, paused.text
    assert paused.json()["executable"] is False
    assert paused.json()["blocked_reason"] == "pair_not_coverable"

    # fx_enabled=false is the total user-trading stop: both actions blocked.
    await _seed_config(fx_enabled="false")
    for payload in ({"action": "open", "foreign_amount": "10"},
                    {"action": "cover", "cover_all": True}):
        response = await _quote(client, user_headers, pair_id, payload)
        assert response.status_code == 200, (payload, response.text)
        body = response.json()
        assert body["executable"] is False
        assert body["blocked_reason"] == "fx_disabled"


# ── scenario 7: order eligibility vs risk/valuation reason (fix round 1) ─────

async def test_frozen_cover_quote_separates_order_from_risk_reason_and_executes(client):
    """A frozen holder's cover is executable; freeze is a risk reason only.

    Regression: ``blocked_reason`` carrying the risk simulation's ``credit_frozen``
    while ``executable`` is true, so a client that reads ``blocked_reason``
    disables the one action that can repay the debt; or the freeze/unknown-K
    information being dropped instead of surfaced in ``risk_blocked_reason``.
    """
    _, admin_headers = await _make_user(superuser=True)
    user_id, user_headers = await _make_user(cash="1000")
    await _seed_config(fx_enabled="true", loan_enabled="true", loan_daily_rate="0")
    pair_id = await _create_pair(client, admin_headers, gold_reserve="1000",
                                 foreign_reserve="1000", buy_fee_rate="0.02",
                                 sell_fee_rate="0.01",
                                 short_lending_limit_foreign="1000000")
    await _seed_short_direct(pair_id, user_id, principal="100", restricted="300",
                             accrued=datetime.now(timezone.utc))
    async with async_session_maker() as s:
        async with s.begin():
            user = await s.get(User, user_id)
            user.credit_frozen = True

    quote = await _quote(client, user_headers, pair_id,
                         {"action": "cover", "cover_all": True})
    assert quote.status_code == 200, quote.text
    body = quote.json()
    # Order-level: the real cover route permits this, so no order block.
    assert body["executable"] is True
    assert body["blocked_reason"] is None
    assert D(body["input_amount"]) > ZERO
    # Risk/valuation-level: the simulation short-circuited on the freeze, so the
    # portfolio estimate stays nullable and the reason is still reported.
    assert body["risk_status"] == "blocked"
    assert body["risk_blocked_reason"] == "credit_frozen"
    assert body["estimated_equity"] is None
    assert body["estimated_risk_basis"] is None

    # The write route agrees: the frozen holder can still reduce the debt.
    covered = await _cover(client, user_headers, pair_id, cover_all=True,
                           max_gold_in="1000000", key="k-frozen-cover")
    assert covered.status_code == 200, covered.text
    assert (await _stock(pair_id))["principal"] == ZERO


async def test_read_marks_fx_disabled_order_block_keeping_reference_cost(client):
    """``fx_enabled=false`` makes GET non-executable while K stays numeric.

    Regression: the read advertising an executable cover that the quote and the
    write route both refuse, or hiding the known mathematical reference cost just
    because the total trading stop is on.
    """
    _, admin_headers = await _make_user(superuser=True)
    user_id, user_headers = await _make_user(cash="1000")
    await _seed_config(fx_enabled="true", loan_enabled="true", loan_daily_rate="0")
    pair_id = await _create_pair(client, admin_headers, gold_reserve="1000",
                                 foreign_reserve="1000", buy_fee_rate="0.02",
                                 sell_fee_rate="0.01",
                                 short_lending_limit_foreign="1000000")
    await _seed_short_direct(pair_id, user_id, principal="100", restricted="300",
                             accrued=datetime.now(timezone.utc))

    live = await _short_get(client, user_headers, pair_id)
    assert live.status_code == 200, live.text
    live_body = live.json()
    assert live_body["executable"] is True
    assert live_body["risk_status"] == "ok"
    assert live_body["blocked_reason"] is None
    assert D(live_body["reference_cover_cost"]) > ZERO

    await _seed_config(fx_enabled="false")
    blocked = await _short_get(client, user_headers, pair_id)
    assert blocked.status_code == 200, blocked.text
    body = blocked.json()
    # Known math is preserved: same numeric reference cost and risk status.
    assert D(body["reference_cover_cost"]) == D(live_body["reference_cover_cost"])
    assert body["risk_status"] == "ok"
    # Order eligibility follows the total trading stop.
    assert body["executable"] is False
    assert body["blocked_reason"] == "fx_disabled"

    # Consistent with the quote and the write route (both refuse).
    quote = await _quote(client, user_headers, pair_id,
                         {"action": "cover", "cover_all": True})
    assert quote.status_code == 200, quote.text
    assert quote.json()["executable"] is False
    assert quote.json()["blocked_reason"] == "fx_disabled"
    write = await _cover(client, user_headers, pair_id, cover_all=True,
                         max_gold_in="1000000", key="k-fx-off")
    assert write.status_code == 403, write.text


async def test_read_marks_unified_credit_disabled_matching_quote_and_cover(
        client, monkeypatch):
    """Unified credit off makes GET non-executable while the known math stays.

    Regression: a known, coverable short advertised as ``executable`` while the
    quote route and the cover write both refuse with ``unified_credit_disabled``,
    or the numeric reference cost / ``risk_status`` being lost because the order
    gate fired.  Models the invalid startup combination (live debt + unified
    credit off) that WP5 will refuse; read/quote must still agree meanwhile.
    """
    _, admin_headers = await _make_user(superuser=True)
    user_id, user_headers = await _make_user(cash="1000")
    await _seed_config(fx_enabled="true", loan_enabled="true", loan_daily_rate="0")
    pair_id = await _create_pair(client, admin_headers, gold_reserve="1000",
                                 foreign_reserve="1000", buy_fee_rate="0.02",
                                 sell_fee_rate="0.01",
                                 short_lending_limit_foreign="1000000")
    await _seed_short_direct(pair_id, user_id, principal="100", restricted="300",
                             accrued=datetime.now(timezone.utc))

    live = await _short_get(client, user_headers, pair_id)
    assert live.status_code == 200, live.text
    live_body = live.json()
    assert live_body["executable"] is True
    assert live_body["blocked_reason"] is None
    assert D(live_body["reference_cover_cost"]) > ZERO

    # Process-global credit flag off while a live short exists.
    monkeypatch.setattr(credit_flags, "get_flags",
                        lambda: credit_flags.parse_flags({}))

    blocked = await _short_get(client, user_headers, pair_id)
    assert blocked.status_code == 200, blocked.text
    body = blocked.json()
    # Known math and risk status survive the order gate.
    assert D(body["reference_cover_cost"]) == D(live_body["reference_cover_cost"])
    assert body["risk_status"] == "ok"
    assert body["executable"] is False
    assert body["blocked_reason"] == "unified_credit_disabled"

    # Same order verdict as the quote and the write route.
    quote = await _quote(client, user_headers, pair_id,
                         {"action": "cover", "cover_all": True})
    assert quote.status_code == 200, quote.text
    assert quote.json()["executable"] is False
    assert quote.json()["blocked_reason"] == "unified_credit_disabled"
    write = await _cover(client, user_headers, pair_id, cover_all=True,
                         max_gold_in="1000000", key="k-unified-off")
    assert write.status_code == 403, write.text

    # The total trading stop keeps precedence over the sibling credit gate.
    await _seed_config(fx_enabled="false")
    total_stop = await _short_get(client, user_headers, pair_id)
    assert total_stop.status_code == 200, total_stop.text
    assert total_stop.json()["executable"] is False
    assert total_stop.json()["blocked_reason"] == "fx_disabled"


async def test_other_pair_unknown_k_blocks_only_the_risk_reason(client):
    """Another pair's unquotable debt is a risk reason, not this order's block.

    Regression: a small, affordable fixed-q cover on this pair being reported as
    non-executable because a different pair's K cannot be priced, or the
    unknown-K reason being erased once ``blocked_reason`` is reserved for orders.
    """
    _, admin_headers = await _make_user(superuser=True)
    user_id, user_headers = await _make_user(cash="100000")
    await _seed_config(fx_enabled="true", loan_enabled="true", loan_daily_rate="0")
    good = await _create_pair(client, admin_headers, gold_reserve="1000",
                              foreign_reserve="1000", buy_fee_rate="0.02",
                              sell_fee_rate="0.01",
                              short_lending_limit_foreign="1000000")
    # q >= foreign_reserve on the second pair makes the whole-portfolio K unknown.
    dead = await _create_pair(client, admin_headers, currency_code="MRO",
                              gold_reserve="1000", foreign_reserve="50",
                              buy_fee_rate="0.02", sell_fee_rate="0.01",
                              short_lending_limit_foreign="1000000")
    await _seed_short_direct(good, user_id, principal="10", restricted="30",
                             accrued=datetime.now(timezone.utc))
    await _seed_short_direct(dead, user_id, principal="100", restricted="300",
                             accrued=datetime.now(timezone.utc))

    quote = await _quote(client, user_headers, good,
                         {"action": "cover", "foreign_amount": "5"})
    assert quote.status_code == 200, quote.text
    body = quote.json()
    assert body["executable"] is True
    assert body["blocked_reason"] is None
    assert body["risk_status"] == "blocked"
    assert body["risk_blocked_reason"] == "insufficient_pool_foreign"
    assert body["estimated_equity"] is None
    assert body["estimated_risk_basis"] is None


# ── scenario 8: complete post-cover valuation (WP3 stage finding) ─────────────

async def test_cover_all_quote_includes_uncached_other_asset_in_equity_and_basis(client):
    """Post-cover E/B must price every positive holding, not just cached ones.

    Regression: a full cover that removes the last short takes the risk engine's
    no-debt fast path, which values uncached positive holdings at zero and omits
    ``risk_basis``.  After a restart (empty risk LRU) a user holding another spot
    FX asset would see ``risk_status="ok"`` with an equity that silently drops
    that asset and a missing B.  The quote must instead value the exact
    post-order state with the shared A/K/E/B primitives.
    """
    _, admin_headers = await _make_user(superuser=True)
    user_id, user_headers = await _make_user(cash="1000")
    await _seed_config(fx_enabled="true", loan_enabled="true", loan_daily_rate="0")
    short_pair = await _create_pair(client, admin_headers, gold_reserve="1000",
                                    foreign_reserve="1000", buy_fee_rate="0",
                                    sell_fee_rate="0",
                                    short_lending_limit_foreign="0")
    asset_pair = await _create_pair(client, admin_headers, currency_code="EUR",
                                    gold_reserve="1000", foreign_reserve="1000",
                                    buy_fee_rate="0", sell_fee_rate="0",
                                    short_lending_limit_foreign="0")
    await _seed_short_direct(short_pair, user_id, principal="100", restricted="300",
                             accrued=datetime.now(timezone.utc))
    await _seed_wallet(asset_pair, user_id, "100")
    # Reproduce the post-restart / asset-version-change cache state explicitly:
    # the other positive group has no cached group value.
    credit_risk.clear_risk_cache()

    quote = await _quote(client, user_headers, short_pair,
                         {"action": "cover", "cover_all": True})
    assert quote.status_code == 200, quote.text
    body = quote.json()
    assert body["executable"] is True
    assert body["risk_status"] == "ok", body
    assert body["risk_blocked_reason"] is None

    expected_buy = quote_buy_exact_out(D("100"), D("1000"), D("1000"), D("0"))
    expected_cash = D("1000") - expected_buy.input_amount
    expected_asset = quote_sell(
        D("100"), D("1000"), D("1000"), D("0")).output_amount
    alpha = credit_flags.get_flags().thresholds.alpha
    expected_basis = (alpha * expected_asset).quantize(Q6, rounding=ROUND_CEILING)

    # The regression: a cache-only fast path reports only post-cover cash.
    assert D(body["estimated_equity"]) > expected_cash
    assert D(body["estimated_equity"]) == expected_cash + expected_asset
    assert D(body["estimated_risk_basis"]) == expected_basis
    assert D(body["estimated_risk_basis"]) > ZERO

    # The advisory quote equals the persisted post-cover valuation: one model.
    covered = await _cover(client, user_headers, short_pair, cover_all=True,
                           max_gold_in="1000000", key="k-uncached-cover")
    assert covered.status_code == 200, covered.text
    async with async_session_maker() as s:
        actual = await value_user_detailed(s, user_id, daily_rate=D("0"))
    assert actual.liquidation_equity == D(body["estimated_equity"])
    assert actual.risk_basis == D(body["estimated_risk_basis"])


async def test_cover_quote_gold_debt_basis_is_max_debt_alpha_assets_and_frozen_kept(
        client):
    """With gold debt, post-cover B is ``max(D, αA)``, never just D/None.

    Regression: eliminating the last short routes through the legacy no-short
    branch, which omits ``risk_basis`` (or implies ``B=D``) even when the user's
    own positive assets dominate ``αA > D``; a frozen holder must still get an
    executable cover with a null estimate and the freeze as the risk reason.
    """
    _, admin_headers = await _make_user(superuser=True)
    user_id, user_headers = await _make_user(cash="1000")
    await _seed_config(fx_enabled="true", loan_enabled="true", loan_daily_rate="0")
    short_pair = await _create_pair(client, admin_headers, gold_reserve="1000",
                                    foreign_reserve="1000", buy_fee_rate="0",
                                    sell_fee_rate="0",
                                    short_lending_limit_foreign="0")
    asset_pair = await _create_pair(client, admin_headers, currency_code="EUR",
                                    gold_reserve="1000", foreign_reserve="1000",
                                    buy_fee_rate="0", sell_fee_rate="0",
                                    short_lending_limit_foreign="0")
    await _seed_short_direct(short_pair, user_id, principal="100", restricted="300",
                             accrued=datetime.now(timezone.utc))
    await _seed_wallet(asset_pair, user_id, "1000")
    async with async_session_maker() as s:
        async with s.begin():
            user = await s.get(User, user_id)
            user.debt = D("50")
    credit_risk.clear_risk_cache()

    quote = await _quote(client, user_headers, short_pair,
                         {"action": "cover", "cover_all": True})
    assert quote.status_code == 200, quote.text
    body = quote.json()
    assert body["executable"] is True
    assert body["risk_status"] == "ok", body

    expected_buy = quote_buy_exact_out(D("100"), D("1000"), D("1000"), D("0"))
    expected_cash = D("1000") - expected_buy.input_amount
    expected_asset = quote_sell(
        D("1000"), D("1000"), D("1000"), D("0")).output_amount
    alpha = credit_flags.get_flags().thresholds.alpha
    expected_basis = max(D("50"), alpha * expected_asset).quantize(
        Q6, rounding=ROUND_CEILING)
    assert expected_basis > D("50")  # assets dominate, so B is not just debt
    assert D(body["estimated_equity"]) == expected_cash + expected_asset - D("50")
    assert D(body["estimated_risk_basis"]) == expected_basis

    # Existing frozen semantics survive: reduce-only cover stays executable with
    # nullable estimates and the freeze reported as the risk reason only.
    async with async_session_maker() as s:
        async with s.begin():
            user = await s.get(User, user_id)
            user.credit_frozen = True
    frozen = await _quote(client, user_headers, short_pair,
                          {"action": "cover", "cover_all": True})
    assert frozen.status_code == 200, frozen.text
    frozen_body = frozen.json()
    assert frozen_body["executable"] is True
    assert frozen_body["blocked_reason"] is None
    assert frozen_body["risk_status"] == "blocked"
    assert frozen_body["risk_blocked_reason"] == "credit_frozen"
    assert frozen_body["estimated_equity"] is None
    assert frozen_body["estimated_risk_basis"] is None
