"""Admin-only, immediate configuration without returning or auditing secrets."""
import hashlib

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.main import app
from app.models.audit import AuditEvent
from app.models.base import SiteConfig, User

PATH = '/api/v1/admin/returned/config'
KEY = 'test-returned-config-secret-32-characters'
CONFIG = dict(enabled=True, returned_url='https://returned.example', secret=KEY)


@pytest_asyncio.fixture
async def config_api():
    async with async_session_maker() as db:
        admin = User(username='config-admin', is_superuser=True)
        viewer = User(username='config-viewer')
        db.add_all([admin, viewer])
        await db.commit()
        headers = {'Authorization': f'Bearer {create_access_token(admin.id)}'}
        viewer_headers = {'Authorization': f'Bearer {create_access_token(viewer.id)}'}
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        yield client, headers, viewer_headers


@pytest.mark.asyncio
async def test_admin_settings_take_effect_and_secret_never_returns(config_api):
    api, headers, viewer = config_api
    for method in (api.get, api.put):
        kwargs = {'json': CONFIG} if method == api.put else {}
        assert (await method(PATH, **kwargs)).status_code in (401, 403)
        assert (await method(PATH, headers=viewer, **kwargs)).status_code == 403
    assert (await api.get('/api/v1/returned/current')).status_code == 404
    assert (await api.put(PATH, json=CONFIG, headers=headers)).status_code == 200
    body = (await api.get(PATH, headers=headers)).json()
    assert body['enabled'] is True and body['secret_configured'] is True
    assert body['returned_url'] == CONFIG['returned_url']
    assert 'secret' not in body
    assert (await api.get('/api/v1/returned/current')).status_code == 200
    unchanged = await api.put(PATH, json={**CONFIG, 'secret': ''}, headers=headers)
    assert unchanged.status_code == 200
    source = {'Authorization': f'Bearer {KEY}'}
    assert (await api.post('/api/v1/returned/rounds/missing/close', headers=source)).status_code == 404
    async with async_session_maker() as db:
        rows = (await db.execute(select(SiteConfig))).scalars().all()
        events = (await db.execute(select(AuditEvent))).scalars().all()
        assert all(KEY not in row.value for row in rows)
        stored = {row.key: row.value for row in rows}
        assert stored['wavelength_url'] == CONFIG['returned_url']
        assert stored['wavelength_secret_hash'] == hashlib.sha256(KEY.encode()).hexdigest()
        assert all(KEY not in str(event.payload) for event in events)
        digest = hashlib.sha256(KEY.encode()).hexdigest()
        assert all(digest not in str(event.payload) for event in events)
    listing = (await api.get('/api/v1/admin/site-config', headers=headers)).text
    assert KEY not in listing and digest not in listing
    assert (await api.put(PATH, json={**CONFIG, 'enabled': False, 'secret': ''}, headers=headers)).status_code == 200
    assert (await api.get('/api/v1/returned/current')).status_code == 404


@pytest.mark.asyncio
async def test_settings_validate_key_url_and_refuse_active_round_changes(config_api):
    api, headers, viewer = config_api
    invalid = await api.put(PATH, json={**CONFIG, 'secret': 'private-short-key'}, headers=headers)
    assert invalid.status_code == 422 and 'private-short-key' not in invalid.text
    assert (await api.put(PATH, json={**CONFIG, 'secret': ''}, headers=headers)).status_code == 422
    assert (await api.put(PATH, json={**CONFIG, 'returned_url': 'https://user:password@example.org'}, headers=headers)).status_code == 422
    assert (await api.put(PATH, json=CONFIG, headers=headers)).status_code == 200
    source = {'Authorization': f'Bearer {KEY}'}
    started = await api.post('/api/v1/returned/rounds/start', headers=source, json={
        'round_key': 'active', 'game_key': 'game', 'round_number': 1,
        'left_label': 'left', 'right_label': 'right'})
    assert started.status_code == 200
    changed = await api.put(PATH, json={**CONFIG, 'enabled': False, 'secret': ''}, headers=headers)
    assert changed.status_code == 409
    assert (await api.put(PATH, json={**CONFIG, 'secret': ''}, headers=headers)).status_code == 200
    cancel_path = '/api/v1/admin/returned/rounds/active/cancel'
    assert (await api.post(cancel_path, headers=viewer)).status_code == 403
    assert (await api.post(cancel_path, headers=headers)).status_code == 200
    assert (await api.put(PATH, json={**CONFIG, 'enabled': False, 'secret': ''}, headers=headers)).status_code == 200
