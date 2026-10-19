"""Paid audience predictions: auth, money, lifecycle and audit consistency."""
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from sqlalchemy import select
from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.main import app
from app.models.base import SiteConfig, User
from app.services import audit_replay, audit_service
from app.services.returned_config import secret_digest

BASE = '/api/v1/returned'
KEY = 'returned-test-secret-32-characters'
SOURCE = {'Authorization': f'Bearer {KEY}'}
TOPIC = dict(round_key='game-1-round-1', game_key='game-1', round_number=1,
             left_label='寒冷', right_label='炎热', team_name='红队')


@pytest_asyncio.fixture
async def api():
    await config_value('wavelength_enabled', 'true')
    await config_value('wavelength_secret_hash', secret_digest(KEY))
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        yield client


async def config_value(key, value):
    async with async_session_maker() as db:
        row = (await db.execute(select(SiteConfig).where(SiteConfig.key == key))).scalar_one_or_none()
        if row is None:
            row = SiteConfig(key=key, value=value, value_type='string')
            db.add(row)
        row.value = value
        await db.commit()


async def player(name='audience', cash='500', debt='0', frozen=False):
    async with async_session_maker() as db:
        user = User(username=name, cash=Decimal(cash), debt=Decimal(debt), credit_frozen=frozen)
        db.add(user)
        await db.flush()
        audit_service.record(db, 'user_register', user_id=user.id,
                             user_after=audit_service.user_snapshot(user))
        uid = user.id
        await db.commit()
    return uid, {'Authorization': f'Bearer {create_access_token(uid)}'}


async def balance(uid):
    async with async_session_maker() as db:
        return (await db.get(User, uid)).cash


async def start(api, **changes):
    r = await api.post(BASE + '/rounds/start', json={**TOPIC, **changes}, headers=SOURCE)
    assert r.status_code == 200, r.text
    return r.json()


async def predict(api, headers, position, key=TOPIC['round_key']):
    return await api.put(f'{BASE}/rounds/{key}/prediction', json={'position': position}, headers=headers)


@pytest.mark.asyncio
async def test_source_switch_and_key_gate_all_source_writes(api):
    for suffix, body in [('/start', TOPIC), ('/x/close', {}),
                         ('/x/result', {'target_position': 500}), ('/x/cancel', {})]:
        path = BASE + '/rounds' + suffix
        assert (await api.post(path, json=body)).status_code == 401
        await config_value('wavelength_enabled', 'false')
        assert (await api.post(path, json=body, headers=SOURCE)).status_code == 404
        await config_value('wavelength_enabled', 'true')
    assert (await api.post(BASE + '/rounds/start', json=TOPIC,
                          headers={'Authorization': 'Bearer invalid'})).status_code == 401
    _, headers = await player()
    assert (await api.post(BASE + '/rounds/start', json=TOPIC, headers=headers)).status_code == 401
    await config_value('wavelength_secret_hash', '')
    assert (await api.post(BASE + '/rounds/start', json=TOPIC, headers=SOURCE)).status_code == 503


@pytest.mark.asyncio
async def test_first_prediction_charges_once_edit_and_seal(api):
    uid, headers = await player()
    await start(api)
    assert (await predict(api, headers, 1001)).status_code == 422
    assert (await predict(api, headers, 16.5)).status_code == 422
    assert (await predict(api, SOURCE, 100)).status_code == 401
    for position in [100, 200, 200]:
        r = await predict(api, headers, position)
        assert r.status_code == 200, r.text
    assert await balance(uid) == 400
    public = (await api.get(BASE + '/current')).json()
    assert public['target_position'] is None
    assert (await api.post(BASE + '/rounds/game-1-round-1/close', headers=SOURCE)).status_code == 200
    assert (await predict(api, headers, 500)).status_code == 409
    mine = (await api.get(BASE + '/rounds/game-1-round-1/me', headers=headers)).json()
    assert mine['position'] == 200 and Decimal(mine['stake']) == 100
    assert mine['payout'] is None


@pytest.mark.asyncio
async def test_lifecycle_conflicts_and_cancel_refunds_once(api):
    uid, headers = await player()
    original = await start(api)
    assert await start(api) == original
    assert (await api.post(BASE + '/rounds/start', json={**TOPIC, 'left_label': 'changed'}, headers=SOURCE)).status_code == 409
    assert (await api.post(BASE + '/rounds/start', json={**TOPIC, 'round_key': 'another'}, headers=SOURCE)).status_code == 409
    assert (await api.post(BASE + '/rounds/game-1-round-1/result', json={'target_position': 500}, headers=SOURCE)).status_code == 409
    assert (await predict(api, headers, 500)).status_code == 200
    for _ in range(2):
        assert (await api.post(BASE + '/rounds/game-1-round-1/cancel', headers=SOURCE)).status_code == 200
    assert await balance(uid) == 500
    assert (await predict(api, headers, 300)).status_code == 409
    async with async_session_maker() as db:
        snap, errors = audit_replay.fold(await audit_replay.load_events(db), check=True)
        assert errors == []
        assert await audit_replay.compare_with_live(db, snap) == []


@pytest.mark.asyncio
async def test_fixed_reward_boundaries_and_repeated_result_audit(api):
    await start(api)
    cases = [(0, 300), (16, 300), (17, 200), (56, 200), (57, 150),
             (100, 150), (101, 75), (150, 75), (151, 0)]
    participants = []
    for index, (distance, payout) in enumerate(cases):
        uid, headers = await player(f'p{index}')
        assert (await predict(api, headers, 500 + distance)).status_code == 200
        participants.append((uid, headers, distance, payout))
    await api.post(BASE + '/rounds/game-1-round-1/close', headers=SOURCE)
    path = BASE + '/rounds/game-1-round-1/result'
    first = await api.post(path, json={'target_position': 500}, headers=SOURCE)
    assert first.status_code == 200, first.text
    assert (await api.post(path, json={'target_position': 500}, headers=SOURCE)).json() == first.json()
    assert (await api.post(path, json={'target_position': 501}, headers=SOURCE)).status_code == 409
    assert (await api.post(BASE + '/rounds/game-1-round-1/cancel', headers=SOURCE)).status_code == 409
    for uid, headers, distance, payout in participants:
        assert await balance(uid) == 400 + payout
        mine = (await api.get(BASE + '/rounds/game-1-round-1/me', headers=headers)).json()
        assert mine['distance'] == distance and Decimal(mine['payout']) == payout
        assert Decimal(mine['net_profit']) == payout - 100
    async with async_session_maker() as db:
        snap, errors = audit_replay.fold(await audit_replay.load_events(db), check=True)
        assert errors == []
        assert await audit_replay.compare_with_live(db, snap) == []


@pytest.mark.asyncio
async def test_stake_checks_cash_and_existing_risk_without_banning_all_debt(api):
    await start(api)
    for name, cash, debt, frozen, expected in [('poor', '99', '0', False, 422),
                                               ('frozen', '500', '100', True, 409),
                                               ('borrower', '500', '100', False, 200)]:
        uid, headers = await player(name, cash, debt, frozen)
        r = await predict(api, headers, 500)
        assert r.status_code == expected, r.text
        assert await balance(uid) == Decimal(cash) - (100 if expected == 200 else 0)


@pytest.mark.asyncio
async def test_result_failure_rolls_back_all_rewards_and_can_retry(api, monkeypatch):
    from app.services import ledger_service
    await start(api)
    players = [await player('first'), await player('second')]
    for _, headers in players:
        assert (await predict(api, headers, 500)).status_code == 200
    await api.post(BASE + '/rounds/game-1-round-1/close', headers=SOURCE)
    original = ledger_service.record_entry
    calls = 0

    async def fail_second(*args, **kwargs):
        nonlocal calls
        if kwargs['entry_type'] == 'wavelength_reward':
            calls += 1
            if calls == 2:
                raise RuntimeError('injected reward write failure')
        return await original(*args, **kwargs)

    monkeypatch.setattr(ledger_service, 'record_entry', fail_second)
    with pytest.raises(RuntimeError, match='injected'):
        await api.post(BASE + '/rounds/game-1-round-1/result', json={'target_position': 500}, headers=SOURCE)
    for uid, _ in players:
        assert await balance(uid) == 400
    assert (await api.get(BASE + '/current')).json()['status'] == 'closed'
    monkeypatch.setattr(ledger_service, 'record_entry', original)
    assert (await api.post(BASE + '/rounds/game-1-round-1/result', json={'target_position': 500}, headers=SOURCE)).status_code == 200
    for uid, _ in players:
        assert await balance(uid) == 700


@pytest.mark.asyncio
async def test_round_stream_notifies_committed_changes(api):
    import json
    from app.api.v1.returned import stream

    class Connection:
        async def is_disconnected(self):
            return False

    response = await stream(Connection())
    frames = response.body_iterator
    try:
        assert b'event: sync' in await anext(frames)
        await start(api)
        frame = await anext(frames)
        assert b'event: round_changed' in frame
        payload = json.loads(frame.decode().split('data: ')[1])
        assert payload == {'round_key': TOPIC['round_key'], 'status': 'open'}
        assert (await api.get(BASE + '/current')).json()['status'] == 'open'
    finally:
        await frames.aclose()


@pytest.mark.asyncio
async def test_results_require_auth_and_publish_only_settled_top_five(api):
    from datetime import datetime, timedelta, timezone
    from app.models.returned import ReturnedPrediction

    await start(api)
    players = []
    for name, position in [('first', 510), ('second', 490), ('third', 530),
                           ('fourth', 560), ('fifth', 610), ('sixth', 700)]:
        uid, headers = await player(name)
        assert (await predict(api, headers, position)).status_code == 200
        players.append((uid, headers))
    async with async_session_maker() as db:
        records = (await db.execute(select(ReturnedPrediction).order_by(ReturnedPrediction.user_id))).scalars().all()
        now = datetime.now(timezone.utc)
        for i, record in enumerate(records):
            record.submitted_at = now + timedelta(seconds=1 - i if i < 2 else i)
        await db.commit()
    path = BASE + '/rounds/' + TOPIC['round_key'] + '/results'
    assert (await api.get(path)).status_code in (401, 403)
    assert (await api.get(path, headers={'Authorization': 'Bearer invalid'})).status_code == 401
    assert (await api.get(path, headers=SOURCE)).status_code == 409
    await api.post(BASE + '/rounds/' + TOPIC['round_key'] + '/close', headers=SOURCE)
    assert (await api.get(path, headers=players[0][1])).status_code == 409
    await api.post(BASE + '/rounds/' + TOPIC['round_key'] + '/result',
                   json={'target_position': 500}, headers=SOURCE)
    result = await api.get(path, headers=SOURCE)
    assert result.status_code == 200, result.text
    body = result.json()
    assert body['round_key'] == TOPIC['round_key'] and body['target_position'] == 500
    assert [(p['username'], p['position'], p['distance'], Decimal(p['payout'])) for p in body['top_five']] == [
        ('second', 490, 10, Decimal('300')), ('first', 510, 10, Decimal('300')),
        ('third', 530, 30, Decimal('200')), ('fourth', 560, 60, Decimal('150')),
        ('fifth', 610, 110, Decimal('75'))]
    assert (await api.get(path, headers=players[0][1])).json() == body
    async with async_session_maker() as db:
        user = await db.get(User, players[0][0]); user.is_active = False
        await db.commit()
    assert (await api.get(path, headers=players[0][1])).status_code == 403
    await start(api, round_key='empty')
    await api.post(BASE + '/rounds/empty/close', headers=SOURCE)
    await api.post(BASE + '/rounds/empty/result', json={'target_position': 500}, headers=SOURCE)
    assert (await api.get(BASE + '/rounds/empty/results', headers=SOURCE)).json()['top_five'] == []
    await start(api, round_key='cancelled')
    await api.post(BASE + '/rounds/cancelled/cancel', headers=SOURCE)
    assert (await api.get(BASE + '/rounds/cancelled/results', headers=SOURCE)).status_code == 409
    await config_value('wavelength_enabled', 'false')
    assert (await api.get(path, headers=SOURCE)).status_code == 404
