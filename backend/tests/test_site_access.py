"""The operator gate blocks real API requests without locking administrators out."""
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.core.config import settings
from app.core.database import async_session_maker
from app.core.users import create_access_token, create_refresh_token
from app.models.base import User

KEY = 'site_access_disabled'


async def identity(*, admin=False):
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        user = User(username=f'gate_{suffix}', email=f'{suffix}@test.com',
                    casdoor_id=suffix, is_superuser=admin)
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user.id, {'Authorization': f'Bearer {create_access_token(user.id)}'}


async def enable(client):
    uid, headers = await identity(admin=True)
    response = await client.put(f'/api/v1/admin/site-config/{KEY}',
                                json={'value': 'true'}, headers=headers)
    assert response.status_code == 200, response.text
    return uid, headers


@pytest.mark.asyncio
async def test_missing_gate_defaults_off_and_is_editable(client):
    _, headers = await identity(admin=True)
    status = await client.get('/api/v1/site/access')
    assert status.status_code == 200
    assert status.json() == {'disabled': False}
    configs = (await client.get('/api/v1/admin/site-config', headers=headers)).json()
    assert next(item for item in configs if item['key'] == KEY)['value'] == 'false'
    assert (await client.get('/api/v1/site/homepage')).status_code == 200


@pytest.mark.asyncio
async def test_gate_blocks_reads_writes_and_new_streams_until_admin_reopens(client):
    _, user_headers = await identity()
    _, admin_headers = await enable(client)
    for headers in ({}, user_headers):
        for method, path in [('GET', '/api/v1/site/homepage'),
                             ('GET', '/api/v1/user/trading-context'),
                             ('POST', '/api/v1/market/buy'),
                             ('GET', '/history/o/1/1m/0.json'),
                             ('GET', '/api/v1/stream/market/1')]:
            response = await client.request(method, path, headers=headers)
            assert response.status_code == 403, (path, response.text)
            assert response.json()['detail'] == 'SITE_ACCESS_DISABLED'
            assert response.headers['cache-control'] == 'no-store'
    assert (await client.get('/api/v1/site/homepage', headers=admin_headers)).status_code == 200
    response = await client.put(f'/api/v1/admin/site-config/{KEY}',
                                json={'value': 'false'}, headers=admin_headers)
    assert response.status_code == 200
    assert (await client.get('/api/v1/site/homepage')).status_code == 200


@pytest.mark.asyncio
async def test_authentication_and_status_remain_reachable(client):
    uid, headers = await enable(client)
    assert (await client.get('/api/v1/site/access')).json() == {'disabled': True}
    assert (await client.get('/api/v1/auth/me', headers=headers)).status_code == 200
    assert (await client.post('/api/v1/auth/login-start')).status_code == 200
    assert (await client.post('/api/v1/auth/callback', json={})).status_code == 422
    response = await client.post('/api/v1/auth/refresh',
                                 json={'refresh_token': create_refresh_token(uid)})
    assert response.status_code == 200
    assert (await client.get('/health')).status_code == 200


@pytest.mark.asyncio
async def test_expired_admin_can_renew_and_admin_bypass_uses_current_db_role(client):
    uid, headers = await enable(client)
    expired = jwt.encode({'sub': str(uid), 'type': 'access',
                          'exp': datetime.now(timezone.utc) - timedelta(seconds=1)},
                         settings.SECRET_KEY, algorithm='HS256')
    response = await client.get('/api/v1/site/homepage',
                                headers={'Authorization': f'Bearer {expired}'})
    assert response.status_code == 401
    refreshed = await client.post('/api/v1/auth/refresh',
                                  json={'refresh_token': create_refresh_token(uid)})
    assert refreshed.status_code == 200
    async with async_session_maker() as db:
        user = await db.get(User, uid)
        user.is_superuser = False
        await db.commit()
    response = await client.get('/api/v1/site/homepage', headers=headers)
    assert response.status_code == 403
    assert response.json()['detail'] == 'SITE_ACCESS_DISABLED'


@pytest.mark.asyncio
async def test_regular_user_cannot_change_gate(client):
    _, headers = await identity()
    response = await client.put(f'/api/v1/admin/site-config/{KEY}',
                                json={'value': 'true'}, headers=headers)
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_legacy_admin_login_keeps_its_signed_session(client):
    _, headers = await enable(client)
    token = headers['Authorization'].removeprefix('Bearer ')
    try:
        response = await client.post('/api/v1/admin/login',
                                     data={'username': token, 'password': ''})
        assert response.status_code == 302
        response = await client.get('/api/v1/admin/')
        assert response.status_code == 200
    finally:
        client.cookies.clear()
