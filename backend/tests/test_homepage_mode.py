import pytest
from fastapi import HTTPException
from app.api.v1.site_config import _validate
from app.core.database import async_session_maker
from app.models.base import SiteConfig, User
from app.core.users import create_access_token


def test_homepage_mode_accepts_only_boolean():
    _validate('homepage_fx_enabled', 'true')
    _validate('homepage_fx_enabled', 'false')
    with pytest.raises(HTTPException):
        _validate('homepage_fx_enabled', 'fx')


@pytest.mark.asyncio
async def test_public_homepage_mode_defaults_to_fx_and_reads_saved_selection(client):
    response = await client.get('/api/v1/site/homepage')
    assert response.status_code == 200
    assert response.json() == {'mode': 'fx'}
    async with async_session_maker() as session:
        session.add(SiteConfig(key='homepage_fx_enabled', value='false', value_type='bool'))
        await session.commit()
    response = await client.get('/api/v1/site/homepage')
    assert response.json() == {'mode': 'prediction'}
    denied = await client.put('/api/v1/admin/site-config/homepage_fx_enabled', json={'value': 'true'})
    assert denied.status_code == 401
    async with async_session_maker() as session:
        admin = User(username='homepage-admin', casdoor_id='homepage-admin', is_superuser=True)
        session.add(admin)
        await session.commit()
        await session.refresh(admin)
        headers = {'Authorization': f'Bearer {create_access_token(admin.id)}'}
    for value, mode in [('true', 'fx'), ('false', 'prediction')]:
        saved = await client.put('/api/v1/admin/site-config/homepage_fx_enabled',
                                 json={'value': value}, headers=headers)
        assert saved.status_code == 200, saved.text
        assert (await client.get('/api/v1/site/homepage')).json() == {'mode': mode}
