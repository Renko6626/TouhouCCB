"""Administrator hub routes must enforce auth and act on explicit selections."""
from decimal import Decimal

import pytest
from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import User

pytestmark = pytest.mark.asyncio

async def users():
    async with async_session_maker() as db:
        admin=User(username='hub-admin',is_superuser=True)
        normal=User(username='hub-normal',cash=Decimal('100'))
        banned=User(username='hub-banned',cash=Decimal('100'),is_active=False)
        db.add_all([admin,normal,banned])
        await db.commit()
        return admin.id,normal.id,banned.id

def headers(uid):
    return {'Authorization': f'Bearer {create_access_token(uid)}'}

async def test_hub_reads_require_admin_and_can_inspect_disabled_users(client):
    admin,normal,banned=await users()
    r=await client.get(f'/api/v1/admin/users/{banned}/activity',headers=headers(normal))
    assert r.status_code==403
    r=await client.get(f'/api/v1/admin/users/{banned}/activity',headers=headers(admin))
    assert r.status_code==200,r.text
    assert r.json()['positions']==[]
    r=await client.get('/api/v1/admin/users',params={'user_ids':str(banned),'is_active':'false'},headers=headers(admin))
    assert r.status_code==200,r.text
    assert [u['id'] for u in r.json()]==[banned]
    assert r.json()[0]['short_count']==0

async def test_batch_route_auth_validation_preview_and_explicit_selection(client):
    admin,normal,banned=await users()
    payload={'user_ids':[normal],'action':'ban','reason':'人工复核','dry_run':True}
    r=await client.post('/api/v1/admin/users/batch/accounts',headers=headers(normal),json=payload)
    assert r.status_code==403
    r=await client.post('/api/v1/admin/users/batch/accounts',headers=headers(admin),json={**payload,'reason':'  '})
    assert r.status_code in (400,422)
    r=await client.post('/api/v1/admin/users/batch/accounts',headers=headers(admin),json=payload)
    assert r.status_code==200,r.text
    assert r.json()['items'][0]['status']=='ready'
    async with async_session_maker() as db:
        assert (await db.get(User,normal)).is_active
    r=await client.post('/api/v1/admin/users/batch/accounts',headers=headers(admin),json={**payload,'dry_run':False})
    assert r.status_code==200,r.text
    assert r.json()['items'][0]['status']=='success'
    async with async_session_maker() as db:
        assert not (await db.get(User,normal)).is_active
        assert not (await db.get(User,banned)).is_active
        assert (await db.get(User,admin)).is_active

async def test_existing_cash_tool_uses_only_selected_ids(client):
    admin,normal,banned=await users()
    r=await client.post('/api/v1/admin/users/batch/adjust-cash',headers=headers(admin),json={
        'filter':{'user_ids':[banned]},'amount':'25','reason':'定向补偿','dry_run':False,
    })
    assert r.status_code==200,r.text
    assert [u['user_id'] for u in r.json()['updated']]==[banned]
    async with async_session_maker() as db:
        assert (await db.get(User,normal)).cash==Decimal('100')
        assert (await db.get(User,banned)).cash==Decimal('125')
