"""Task 3c1: authenticated FX short write routes, wrappers and operator gates.

Real HTTP + persisted-state scenarios against the FastAPI app, the real gate
registry, the real AMM and the real SQLite database.  They assert actual
cash / pool / treasury / wallet / short-debt / trade postconditions, never
source text or private helper names.

A regression here means: an operator can open shorts while the site gate or
pair lending cap is off; a same-key retry borrows/sells/buys twice or lets a
spot request replay a short trade (in either direction); a reduce-only pair
still lets an ordinary buy or a new short through; turning the opening gate or
the loan gate off strands an existing short with no cover path; a malformed or
overflowing request becomes a 500 or mutates the book; or the admin pair limit
leaks into the public pair payload.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.audit import AuditEvent
from app.models.base import SiteConfig, User
from app.models.fx import FxPair, FxShortPosition, FxTrade, FxTreasury, FxWallet
from app.services import site_config
from app.services.credit import flags as credit_flags
from app.services.credit.gates import GATES

pytestmark = pytest.mark.asyncio

ZERO = D("0")

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


async def _seed_overflow_short(pair_id, user_id, *, principal="999999999999999000"):
    async with async_session_maker() as s:
        async with s.begin():
            s.add(FxShortPosition(
                user_id=user_id, pair_id=pair_id,
                principal_foreign=D(principal), interest_foreign=ZERO,
                interest_last_accrued_at=datetime.now(timezone.utc) - timedelta(days=400),
                restricted_gold=ZERO, proceeds_basis_gold=ZERO,
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
