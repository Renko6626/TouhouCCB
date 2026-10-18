from tests.fx_test_helpers import committed_fx_registry, assert_committed_fx
from decimal import Decimal
import json

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.api.v1.admin_fx import router
from app.api.v1.fx import router as public_router
from app.core.database import get_async_session
from app.core.users import current_active_user, current_superuser
from app.models.audit import AuditEvent
from app.models.base import User
from app.models.fx import FxPoolEvent, FxPair, FxTrade, FxTreasury, FxWallet
from app.services.credit.ownership import EconomicWritesDisabled, WriteOwnership
from tests.fx_test_helpers import fx_db


@pytest_asyncio.fixture
async def ctx(fx_db, monkeypatch):
    owner = WriteOwnership(url="sqlite+aiosqlite:///:memory:")
    await owner.acquire()
    for module in ("app.api.v1.admin_fx", "app.services.fx.liquidity"):
        monkeypatch.setattr(f"{module}.OWNERSHIP", owner)
    await fx_db.execute(text("PRAGMA foreign_keys=ON"))
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
    application.dependency_overrides[current_active_user] = admin_override
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
        yield client, fx_db, application, admin, normal
    await owner.release()


PAIR = {"currency_code": "USD", "currency_name": "Dollar", "status": "trading",
        "gold_reserve": "100", "foreign_reserve": "100", "initial_price": "1", }


@pytest.mark.asyncio
async def test_announcement_append_latest_and_live_update(ctx, monkeypatch):
    from app.api.v1 import admin_fx
    from app.services.realtime import MarketEventBroker
    from tests.fx_test_helpers import add_pair

    client, db, _, admin, _ = ctx
    broker = MarketEventBroker()
    monkeypatch.setattr(admin_fx, "BROKER", broker, raising=False)
    first_pair, _ = await add_pair(db, code="ANN1")
    second_pair, _ = await add_pair(db, code="ANN2")
    second_pair.status = "paused"
    await db.commit()
    first_sub, _ = await broker.subscribe(f"fx:{first_pair.id}")
    second_sub, _ = await broker.subscribe(f"fx:{second_pair.id}")
    try:
        empty = await client.get("/api/v1/fx/announcements/latest")
        assert empty.status_code == 200 and empty.json() is None
        first = await client.post("/api/v1/admin/fx/announcements", json={"content": "  决赛结束后\n公布注资结果。  "})
        assert first.status_code == 201, first.text
        assert first.json()["content"] == "决赛结束后 公布注资结果。"
        assert set(first.json()) == {"id", "content", "created_at"}
        for sub in (first_sub, second_sub):
            event = json.loads(sub.q.get_nowait().decode().split("data: ", 1)[1])
            assert event["type"] == "fx_announcement"
            assert event["data"]["id"] == first.json()["id"]
        second = await client.post("/api/v1/admin/fx/announcements", json={"content": "已向卡牌池注入 100,000 金圆券。"})
        assert second.status_code == 201, second.text
        assert second.json()["id"] > first.json()["id"]
        assert (await client.get("/api/v1/fx/announcements/latest")).json() == second.json()
        from app.models.fx import FxAnnouncement
        rows = (await db.execute(select(FxAnnouncement).order_by(FxAnnouncement.id))).scalars().all()
        assert [r.content for r in rows] == [first.json()["content"], second.json()["content"]]
        assert all(r.created_by == admin.id for r in rows)
    finally:
        await broker.unsubscribe(f"fx:{first_pair.id}", first_sub)
        await broker.unsubscribe(f"fx:{second_pair.id}", second_sub)


@pytest.mark.asyncio
async def test_only_admin_can_append_announcements(ctx):
    client, _, app, _, normal = ctx
    app.state.current_user = normal
    assert (await client.post("/api/v1/admin/fx/announcements", json={"content": "预告"})).status_code == 403
    # Reading the latest announcement is public, even without an admin session.
    assert (await client.get("/api/v1/fx/announcements/latest")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"content": " \n\t "}, {"content": "字" * 281}, {"content": "预告", "title": "标题"}])
async def test_announcement_content_validation(ctx, payload):
    client, _, _, _, _ = ctx
    assert (await client.post("/api/v1/admin/fx/announcements", json=payload)).status_code == 422
    assert (await client.get("/api/v1/fx/announcements/latest")).json() is None


@pytest.mark.asyncio
async def test_superuser_single_trading_pair_and_opened_identity(ctx):
    client, db, app, admin, normal = ctx
    app.state.current_user = normal
    assert (await client.post("/api/v1/admin/fx/pairs", json=PAIR)).status_code == 403
    app.state.current_user = admin
    created = await client.post("/api/v1/admin/fx/pairs", json=PAIR)
    assert created.status_code == 200, created.text
    pair_id = created.json()["id"]
    second = await client.post("/api/v1/admin/fx/pairs", json={**PAIR, "currency_code": "EUR"})
    assert second.status_code == 200
    assert (await client.patch(f"/api/v1/admin/fx/pairs/{pair_id}", json={"currency_name": "New"})).status_code == 200
    assert (await client.patch(f"/api/v1/admin/fx/pairs/{pair_id}", json={"status": "paused"})).status_code == 200
    assert (await client.patch(f"/api/v1/admin/fx/pairs/{pair_id}", json={"currency_code": "EUR"})).status_code == 409
    assert (await client.patch(f"/api/v1/admin/fx/pairs/{pair_id}", json={"reduce_only": None})).status_code == 422
    assert (await db.get(FxPair, pair_id)).currency_code == "USD"


@pytest.mark.asyncio
async def test_initial_issuance_fund_withdraw_and_audit(ctx, committed_fx_registry):
    client, db, _, admin, _ = ctx
    pair_id = (await client.post("/api/v1/admin/fx/pairs", json=PAIR)).json()["id"]
    pair = await db.get(FxPair, pair_id)
    await assert_committed_fx(committed_fx_registry, db, pair_id)
    treasury = (await db.execute(select(FxTreasury).where(FxTreasury.pair_id == pair_id))).scalars().one()
    before_gold = pair.gold_reserve + treasury.gold_balance
    before_foreign = pair.foreign_reserve + treasury.foreign_balance
    assert before_gold == before_foreign == Decimal("200")
    funded = await client.post(f"/api/v1/admin/fx/pairs/{pair_id}/fund", json={"gold_amount": "3", "foreign_amount": "2"})
    assert funded.status_code == 200, funded.text
    await assert_committed_fx(committed_fx_registry, db, pair_id)
    withdrawn = await client.post(f"/api/v1/admin/fx/pairs/{pair_id}/withdraw", json={"gold_amount": "1", "foreign_amount": "1"})
    assert withdrawn.status_code == 200, withdrawn.text
    await assert_committed_fx(committed_fx_registry, db, pair_id)
    pair = await db.get(FxPair, pair_id)
    treasury = (await db.execute(select(FxTreasury).where(FxTreasury.pair_id == pair_id))).scalars().one()
    assert pair.gold_reserve + treasury.gold_balance == before_gold + Decimal("4")
    assert pair.foreign_reserve + treasury.foreign_balance == before_foreign + Decimal("2")
    assert (await client.post(f"/api/v1/admin/fx/pairs/{pair_id}/withdraw", json={"gold_amount": "10000", "foreign_amount": "0"})).status_code == 409
    assert (await client.post(f"/api/v1/admin/fx/pairs/{pair_id}/fund", json={"gold_amount": "0.0000001", "foreign_amount": "0"})).status_code == 422
    audits = (await db.execute(select(AuditEvent).where(AuditEvent.ref_table == "fx_pair", AuditEvent.ref_id == pair_id))).scalars().all()
    assert {a.event_type for a in audits} >= {"fx_fund", "fx_withdraw"}
    assert all(a.operator_user_id == admin.id for a in audits)
    assert any(a.payload.get("action") == "initial_issuance" for a in audits)
    events = (await db.execute(select(FxPoolEvent).where(FxPoolEvent.pair_id == pair_id).order_by(FxPoolEvent.id))).scalars().all()
    assert [e.kind for e in events] == ["initialize", "fund", "withdraw"]
    assert all(e.gold_volume == 0 and e.trade_id is None for e in events)
    assert {e.audit_id for e in events} == {a.id for a in audits if a.event_type in {"fx_fund", "fx_withdraw"}}
    assert events[0].pre_gold_reserve is None and events[0].post_gold_reserve == 100
    assert events[1].pre_gold_reserve == 100 and events[1].post_gold_reserve == 103
    assert events[2].pre_gold_reserve == 103 and events[2].post_gold_reserve == 102
    assert (await client.patch(f"/api/v1/admin/fx/pairs/{pair_id}", json={"status": "paused"})).status_code == 200
    assert len((await db.execute(select(FxPoolEvent).where(FxPoolEvent.pair_id == pair_id))).scalars().all()) == 3
    for event_type in ("fx_fund", "fx_withdraw"):
        row = next(a for a in audits if a.event_type == event_type and "pool_before" in a.payload)
        for section in ("pool_before", "pool_after", "treasury_before", "treasury_after"):
            assert set(row.payload[section]) == {"gold", "foreign"}






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
    # Draft pairs and their treasury balances are readable right after creation.
    assert row["status"] == "draft"
    assert Decimal(row["gold_reserve"]) == Decimal("100")
    assert Decimal(row["gold_balance"]) == Decimal("100")
    assert Decimal(row["foreign_balance"]) == Decimal("100")

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
async def test_delete_unused_pair_cleans_dependents_and_keeps_audit(ctx):
    client, db, app, admin, normal = ctx
    pair_id = (await client.post('/api/v1/admin/fx/pairs', json=PAIR)).json()['id']
    assert (await client.post(f"/api/v1/admin/fx/pairs/{pair_id}/fund", json={"gold_amount": "1", "foreign_amount": "0"})).status_code == 200
    db.add(FxWallet(user_id=admin.id, pair_id=pair_id))
    await db.commit()
    response = await client.delete(f'/api/v1/admin/fx/pairs/{pair_id}')
    assert response.status_code == 204, response.text
    assert await db.get(FxPair, pair_id) is None
    for model in (FxWallet, FxTreasury, FxPoolEvent):
        assert not (await db.execute(select(model).where(model.pair_id == pair_id))).scalars().all()
    assert (await client.get('/api/v1/fx/pairs')).json() == []
    audits = (await db.execute(select(AuditEvent).where(AuditEvent.event_type == 'fx_pair_delete'))).scalars().all()
    funding_audits = (await db.execute(select(AuditEvent).where(AuditEvent.event_type == "fx_fund", AuditEvent.ref_id == pair_id))).scalars().all()
    assert len(funding_audits) == 2
    assert len(audits) == 1 and audits[0].operator_user_id == admin.id
    assert audits[0].payload['before']['currency_code'] == 'USD'
    assert (await client.delete(f'/api/v1/admin/fx/pairs/{pair_id}')).status_code == 404


async def _cleared_trade(db, pair_id, user_id):
    trade = FxTrade(pair_id=pair_id, user_id=user_id, side='sell', input_amount=Decimal('1'),
                    output_amount=Decimal('1'), pre_gold_reserve=Decimal('101'),
                    pre_foreign_reserve=Decimal('99'), post_gold_reserve=Decimal('100'),
                    post_foreign_reserve=Decimal('100'), post_price=Decimal('1'))
    db.add(trade)
    db.add(FxWallet(pair_id=pair_id, user_id=user_id))
    await db.commit()
    return trade


@pytest.mark.asyncio
async def test_archive_cleared_pair_hides_market_and_preserves_history(ctx):
    client, db, app, admin, normal = ctx
    pair_id = (await client.post('/api/v1/admin/fx/pairs', json=PAIR)).json()['id']
    trade = await _cleared_trade(db, pair_id, admin.id)
    assert (await client.delete(f'/api/v1/admin/fx/pairs/{pair_id}')).status_code == 409
    response = await client.post(f'/api/v1/admin/fx/pairs/{pair_id}/archive')
    assert response.status_code == 200, response.text
    assert response.json()['archived'] is True
    assert response.json()['status'] == 'closed'
    assert (await client.get('/api/v1/fx/pairs')).json() == []
    assert (await client.get('/api/v1/admin/fx/pairs')).json()[0]['archived'] is True
    assert (await client.get('/api/v1/fx/my-trades')).json()[0]['id'] == trade.id
    assert (await client.get(f'/api/v1/fx/pairs/{pair_id}/trades')).json()[0]['id'] == trade.id
    assert (await client.patch(f'/api/v1/admin/fx/pairs/{pair_id}', json={'status': 'trading'})).status_code == 409
    assert (await client.post(f'/api/v1/admin/fx/pairs/{pair_id}/fund', json={'gold_amount': '1'})).status_code == 409
    assert await db.get(FxTrade, trade.id) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['archive', 'delete'])
@pytest.mark.parametrize('blocker', ['holding', 'cost_basis'])
async def test_cleanup_blocks_live_holdings(ctx, action, blocker):
    client, db, app, admin, normal = ctx
    pair_id = (await client.post('/api/v1/admin/fx/pairs', json=PAIR)).json()['id']
    if blocker in {'holding', 'cost_basis'}:
        db.add(FxWallet(pair_id=pair_id, user_id=normal.id,
                        foreign_amount=Decimal('1') if blocker == 'holding' else Decimal('0'),
                        cost_basis=Decimal('1') if blocker == 'cost_basis' else Decimal('0')))
    await db.commit()
    url = f'/api/v1/admin/fx/pairs/{pair_id}'
    response = await client.post(url + '/archive') if action == 'archive' else await client.delete(url)
    assert response.status_code == 409, response.text
    assert (await client.get('/api/v1/fx/pairs')).json()[0]['status'] == 'trading'


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['archive', 'delete'])
async def test_cleanup_is_superuser_only_and_read_only_guarded(ctx, action, monkeypatch):
    client, db, app, admin, normal = ctx
    pair_id = (await client.post('/api/v1/admin/fx/pairs', json=PAIR)).json()['id']
    url = f'/api/v1/admin/fx/pairs/{pair_id}'
    async def cleanup():
        return await client.post(url + '/archive') if action == 'archive' else await client.delete(url)
    app.state.current_user = normal
    assert (await cleanup()).status_code == 403
    app.state.current_user = admin
    from app.api.v1.admin_fx import OWNERSHIP
    OWNERSHIP.mark_read_only()
    with pytest.raises(EconomicWritesDisabled):
        await cleanup()
    assert await db.get(FxPair, pair_id) is not None


@pytest.mark.asyncio
async def test_delete_does_not_reuse_pair_id_or_cross_audit_history(ctx):
    client, db, app, admin, normal = ctx
    deleted_id = (await client.post('/api/v1/admin/fx/pairs', json=PAIR)).json()['id']
    assert (await client.delete(f'/api/v1/admin/fx/pairs/{deleted_id}')).status_code == 204
    recreated = await client.post('/api/v1/admin/fx/pairs', json={**PAIR, 'currency_name': 'New market'})
    assert recreated.status_code == 200, recreated.text
    assert recreated.json()['id'] > deleted_id


@pytest.mark.asyncio
async def test_used_pair_cannot_change_code_by_reverting_to_draft(ctx):
    client, db, app, admin, normal = ctx
    pair_id = (await client.post('/api/v1/admin/fx/pairs', json=PAIR)).json()['id']
    await _cleared_trade(db, pair_id, admin.id)
    assert (await client.patch(f'/api/v1/admin/fx/pairs/{pair_id}', json={'status': 'draft'})).status_code == 409
    response = await client.patch(f'/api/v1/admin/fx/pairs/{pair_id}', json={'currency_code': 'EUR'})
    assert response.status_code == 409
    assert (await db.get(FxPair, pair_id)).currency_code == 'USD'


@pytest.mark.asyncio
async def test_edit_draft_duplicate_currency_code_leaves_original_intact(ctx):
    client, db, app, admin, normal = ctx
    pair_id = (await client.post('/api/v1/admin/fx/pairs', json={**PAIR, 'status': 'draft'})).json()['id']
    await client.post('/api/v1/admin/fx/pairs', json={**PAIR, 'currency_code': 'EUR'})
    response = await client.patch(f'/api/v1/admin/fx/pairs/{pair_id}', json={'currency_code': 'EUR'})
    assert response.status_code == 409
    assert (await db.get(FxPair, pair_id)).currency_code == 'USD'




@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['archive', 'delete'])
async def test_cleanup_holds_unified_gate_until_commit(ctx, action, monkeypatch):
    from app.services.credit import flags
    from app.services.credit.keys import GroupKey
    client, db, app, admin, normal = ctx
    pair_id = (await client.post('/api/v1/admin/fx/pairs', json=PAIR)).json()['id']
    settings = flags.parse_flags({'unified_credit_enabled': 'true', 'credit_leverage': '20',
                                  'credit_maintenance_ratio': '0.04'})
    monkeypatch.setattr(flags, 'get_flags', lambda: settings)
    from tests.fx_test_helpers import AsyncCompatSession
    commit = AsyncCompatSession.commit
    gates_at_commit = []
    async def checked_commit(session):
        gates_at_commit.append(__import__('asyncio').current_task().get_name() == f'fx-writer-{pair_id}')
        await commit(session)
    monkeypatch.setattr(AsyncCompatSession, 'commit', checked_commit)
    url = f'/api/v1/admin/fx/pairs/{pair_id}'
    response = await client.post(url + '/archive') if action == 'archive' else await client.delete(url)
    assert response.status_code == (200 if action == 'archive' else 204), response.text
    assert gates_at_commit == [True]


@pytest.mark.asyncio
async def test_opened_unused_pair_cannot_revert_to_draft_and_change_code(ctx):
    client, db, app, admin, normal = ctx
    pair_id = (await client.post('/api/v1/admin/fx/pairs', json=PAIR)).json()['id']
    response = await client.patch(f'/api/v1/admin/fx/pairs/{pair_id}', json={'status': 'draft'})
    assert response.status_code == 409
    assert (await db.get(FxPair, pair_id)).status == 'trading'




@pytest.mark.asyncio
async def test_delete_replay_matches_live_and_keeps_historical_snapshot(ctx):
    from app.services import audit_replay
    client, db, app, admin, normal = ctx
    pair_id = (await client.post('/api/v1/admin/fx/pairs', json=PAIR)).json()['id']
    await client.post(f'/api/v1/admin/fx/pairs/{pair_id}/fund', json={'gold_amount': '10'})
    assert (await client.delete(f'/api/v1/admin/fx/pairs/{pair_id}')).status_code == 204
    events = await audit_replay.load_events(db)
    deletion = next(e for e in events if e.event_type == 'fx_pair_delete')
    historical, errors = audit_replay.fold([e for e in events if e.id < deletion.id], check=True)
    assert errors == []
    assert historical.fx_pairs[pair_id].gold == Decimal('110')
    latest, errors = audit_replay.fold(events, check=True)
    assert errors == []
    assert await audit_replay.compare_with_live(db, latest) == []
    assert pair_id not in latest.fx_pairs




@pytest.mark.asyncio
@pytest.mark.parametrize('principal,interest,basis', [
    ('10', '1', '5'), ('0', '1', '0'),
])
async def test_short_obligations_block_pair_cleanup_but_allow_risk_reduction(ctx, principal, interest, basis):
    from datetime import datetime, timezone
    from app.models.fx import FxShortPosition
    client, db, _, _, user = ctx
    pair_id = (await client.post('/api/v1/admin/fx/pairs', json=PAIR)).json()['id']
    user.cash = Decimal('20')
    short = FxShortPosition(user_id=user.id, pair_id=pair_id,
                            principal_foreign=Decimal(principal), interest_foreign=Decimal(interest),
                            interest_last_accrued_at=datetime.now(timezone.utc),
                            proceeds_basis_gold=Decimal(basis))
    db.add(short)
    await db.commit()
    pair = await db.get(FxPair, pair_id)
    treasury = (await db.execute(select(FxTreasury).where(FxTreasury.pair_id == pair_id))).scalar_one()
    before = (pair.status, pair.archived, pair.pool_version, pair.gold_reserve,
              pair.foreign_reserve, treasury.gold_balance, treasury.foreign_balance)
    audit_ids = [a.id for a in (await db.execute(select(AuditEvent))).scalars()]
    for method, suffix, kwargs in (
        ('patch', '', {'json': {'status': 'closed'}}),
        ('post', '/archive', {}), ('delete', '', {}),
    ):
        response = await getattr(client, method)(f'/api/v1/admin/fx/pairs/{pair_id}{suffix}', **kwargs)
        assert response.status_code == 409, response.text
        assert 'short' in response.json()['detail'].lower()
        pair = await db.get(FxPair, pair_id)
        treasury = (await db.execute(select(FxTreasury).where(FxTreasury.pair_id == pair_id))).scalar_one()
        short = (await db.execute(select(FxShortPosition).where(FxShortPosition.pair_id == pair_id, FxShortPosition.user_id == user.id))).scalar_one()
        assert before == (pair.status, pair.archived, pair.pool_version, pair.gold_reserve,
                          pair.foreign_reserve, treasury.gold_balance, treasury.foreign_balance)
        assert audit_ids == [a.id for a in (await db.execute(select(AuditEvent))).scalars()]
        assert (short.principal_foreign, short.interest_foreign, short.proceeds_basis_gold) == tuple(map(Decimal, (principal, interest, basis)))
    response = await client.patch(f'/api/v1/admin/fx/pairs/{pair_id}',
                                  json={'status': 'paused', 'reduce_only': True})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'paused' and response.json()['reduce_only'] is True
    assert response.json()['pool_version'] > before[2]


@pytest.mark.asyncio
async def test_admin_config_exposes_disabled_short_opening_by_default(ctx):
    client, _, _, _, _ = ctx
    response = await client.get('/api/v1/admin/fx/config')
    assert response.status_code == 200
    assert response.json()['fx_short_enabled'] == 'false'


@pytest.mark.asyncio
async def test_zero_short_row_allows_unused_pair_deletion(ctx):
    from app.models.fx import FxShortPosition
    client, db, _, _, user = ctx
    pair_id = (await client.post('/api/v1/admin/fx/pairs', json=PAIR)).json()['id']
    db.add(FxShortPosition(user_id=user.id, pair_id=pair_id))
    await db.commit()
    response = await client.delete(f'/api/v1/admin/fx/pairs/{pair_id}')
    assert response.status_code == 204, response.text
    assert not (await db.execute(select(FxShortPosition))).scalars().all()
    assert await db.get(FxPair, pair_id) is None


@pytest.mark.asyncio
async def test_admin_overview_preview_and_keyset_history(ctx):
    from datetime import datetime, timezone
    from app.models.fx import FxShortPosition
    client, db, app, admin, normal = ctx
    pair_id = (await client.post("/api/v1/admin/fx/pairs", json=PAIR)).json()["id"]
    other_id = (await client.post("/api/v1/admin/fx/pairs", json={**PAIR, "currency_code": "EUR"})).json()["id"]
    for user, amount in ((admin, 3), (normal, 7)):
        db.add(FxWallet(user_id=user.id, pair_id=pair_id, foreign_amount=amount, cost_basis=amount * 2))
        db.add(FxShortPosition(user_id=user.id, pair_id=pair_id, principal_foreign=amount,
            interest_foreign=1, interest_last_accrued_at=datetime.now(timezone.utc)))
    await db.commit()
    base = f"/api/v1/admin/fx/pairs/{pair_id}"
    overview = (await client.get(base + "/overview")).json()
    assert overview["holdings"] == {"long_users": 2, "long_foreign": "10.000000", "long_cost_gold": "20.000000",
        "short_users": 2, "short_principal_foreign": "10.000000", "short_recorded_interest_foreign": "2.000000",
        "interest_is_realtime": False}
    assert Decimal(overview["reference_price"]) == 1 and overview["as_of"]
    audits_before = list((await db.execute(select(AuditEvent.id))).scalars())
    for amount, expected in (({"gold_amount": "10"}, Decimal("1.1")), ({"foreign_amount": "25"}, Decimal("0.8"))):
        response = await client.post(base + "/liquidity-preview", json={"action": "fund", **amount})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["executable"] and Decimal(data["price_after"]) == expected
        assert Decimal(data["price_change_pct"]) == (expected - 1) * 100
        assert data["pool_after"] == data["treasury_after"]
    exhausted = (await client.post(base + "/liquidity-preview", json={"action": "withdraw", "gold_amount": "100"})).json()
    assert not exhausted["executable"] and exhausted["price_after"] is None
    treasury = (await db.execute(select(FxTreasury).where(FxTreasury.pair_id == pair_id))).scalar_one()
    treasury.gold_balance = Decimal("2")
    await db.commit()
    blocked = (await client.post(base + "/liquidity-preview", json={"action": "withdraw", "gold_amount": "3"})).json()
    assert not blocked["executable"] and "treasury" in blocked["blocked_reason"]
    for invalid in ("0.0000001", "10000000000", "NaN", "-1"):
        assert (await client.post(base + "/liquidity-preview", json={"action": "fund", "gold_amount": invalid})).status_code == 422
    assert list((await db.execute(select(AuditEvent.id))).scalars()) == audits_before
    pair = await db.get(FxPair, pair_id)
    assert pair.gold_reserve == pair.foreign_reserve == 100 and pair.pool_version == 1
    assert (await client.post(base + "/fund", json={"gold_amount": "10"})).status_code == 200
    assert (await client.post(f"/api/v1/admin/fx/pairs/{other_id}/fund", json={"gold_amount": "5"})).status_code == 200
    history = (await client.get(base + "/liquidity-history?limit=1")).json()
    assert len(history["items"]) == 1 and history["items"][0]["action"] == "fund"
    assert Decimal(history["items"][0]["price_change_pct"]) == 10
    initial = (await client.get(base + f'/liquidity-history?limit=1&before_id={history["next_before_id"]}')).json()
    assert initial["next_before_id"] is None
    assert initial["items"][0]["action"] == "initial_issuance"
    assert initial["items"][0]["pool_before"] is initial["items"][0]["price_change_pct"] is None
    assert (await client.get(base + "/liquidity-history?limit=101")).status_code == 422
    overflow = (await client.post(base + "/liquidity-preview", json={"action": "fund", "gold_amount": "9999999999.999999"})).json()
    assert not overflow["executable"] and "storage" in overflow["blocked_reason"]
    db.add(AuditEvent(event_type="fx_fund", ref_table="fx_pair", ref_id=pair_id, payload={"pool_after": {"gold": "unknown"}}))
    await db.commit()
    unknown = (await client.get(base + "/liquidity-history?limit=1")).json()["items"][0]
    assert unknown["gold_amount"] is unknown["pool_before"] is unknown["price_change_pct"] is None
    assert unknown["pool_after"] == {"gold": None, "foreign": None}
    pair = await db.get(FxPair, pair_id)
    pair.archived = True
    await db.commit()
    assert not (await client.post(base + "/liquidity-preview", json={"action": "fund", "gold_amount": "1"})).json()["executable"]
    app.state.current_user = normal
    for path in ("/overview", "/liquidity-history"):
        assert (await client.get(base + path)).status_code == 403
    assert (await client.post(base + "/liquidity-preview", json={"action": "fund", "gold_amount": "1"})).status_code == 403


@pytest.mark.asyncio
async def test_pair_lifecycle_updates_committed_projection(ctx, committed_fx_registry):
    client, db, _, _, _ = ctx
    created = await client.post('/api/v1/admin/fx/pairs', json=PAIR)
    assert created.status_code == 200
    pid = created.json()['id']
    await assert_committed_fx(committed_fx_registry, db, pid)
    for patch in ({'buy_fee_rate': '0.123456785', 'sell_fee_rate': '0.01'},
                  {'status': 'paused', 'reduce_only': True}, {'currency_name': 'Updated'}):
        response = await client.patch(f'/api/v1/admin/fx/pairs/{pid}', json=patch)
        assert response.status_code == 200, response.text
        await assert_committed_fx(committed_fx_registry, db, pid)
    response = await client.post(f'/api/v1/admin/fx/pairs/{pid}/archive')
    assert response.status_code == 200
    await assert_committed_fx(committed_fx_registry, db, pid)
    assert committed_fx_registry.peek_fx(pid).archived
    assert (await client.delete(f'/api/v1/admin/fx/pairs/{pid}')).status_code == 204
    assert committed_fx_registry.peek_fx(pid) is None


@pytest.mark.asyncio
async def test_admin_creation_survives_earlier_missing_cold_read(ctx, committed_fx_registry):
    import asyncio
    client, _, _, _, _ = ctx
    queried, release = asyncio.Event(), asyncio.Event()
    # The empty isolated database allocates the first pair id as 1.
    async def missing_snapshot(db, key):
        queried.set()
        await release.wait()
        return None
    committed_fx_registry._snapshot = missing_snapshot
    reading = asyncio.create_task(committed_fx_registry.read_fx(1))
    await queried.wait()
    try:
        created = await client.post('/api/v1/admin/fx/pairs', json=PAIR)
        assert created.status_code == 200 and created.json()['id'] == 1
    finally:
        release.set()
    assert (await reading).currency_code == PAIR['currency_code']
    assert (await committed_fx_registry.read_fx(1)).currency_code == PAIR['currency_code']
