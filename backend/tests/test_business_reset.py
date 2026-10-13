"""Reset invariants through actual Alembic and real database transactions."""
import os
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, inspect, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from scripts import business_reset as reset
from app.services import audit_replay


@pytest.fixture
def setup_db():
    yield


async def seed(url):
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        def write(c):
            cfg = Config('alembic.ini')
            cfg.attributes['connection'] = c
            command.upgrade(cfg, 'head')
            md = MetaData()
            md.reflect(c)
            now = datetime.now(timezone.utc)
            for i, bot in [(7,False),(9,True),(11,True)]:
                c.execute(md.tables['user'].insert().values(id=i,username=f'private-{i}',is_active=bot,is_superuser=not bot,is_bot=bot,cash=55,debt=2,economic_version=19,credit_frozen=True,tos_accepted_at=now))
            c.execute(md.tables['title'].insert().values(id=4,name='old',description='full',color='#000000',icon='',sort_order=5,is_active=True,created_at=now))
            c.execute(md.tables['user_title'].insert().values(id=17,user_id=9,title_id=4,granted_at=now,granted_by_admin_id=9,source='admin'))
            c.execute(md.tables['title_code_batch'].insert().values(id=20,title_id=4,name='batch',description='',created_at=now,created_by_admin_id=9))
            c.execute(md.tables['title_code'].insert().values(id=21,batch_id=20,code_string='private-code',status='used',used_by_user_id=9,used_at=now))
            c.execute(md.tables['user'].update().where(md.tables['user'].c.id == 7).values(equipped_title_id=4))
            for k,v,t in [('initial_balance','123.456789','decimal'),('credit_leverage','10','decimal'),('credit_maintenance_ratio','0.04','decimal'),('homepage_fx_enabled','false','bool'),('loan_daily_rate','0.02','decimal'),('obsolete','secret','string')]:
                c.execute(md.tables['siteconfig'].insert().values(key=k,value=v,value_type=t,updated_at=now))
            c.execute(md.tables['audit_event'].insert().values(id=99,ts=now,event_type='user_register',user_id=7,payload={},user_after={'cash':'55','debt':'2','debt_last_accrued_at':None}))
            c.execute(md.tables['redemption_partner'].insert().values(id=30,name='private-partner',description='',website_url='',is_active=True,created_at=now))
            c.execute(md.tables['redemption_batch'].insert().values(id=31,partner_id=30,name='batch',description='',unit_price=2,status='active',created_at=now,created_by_admin_id=7))
            c.execute(md.tables['redemption_code'].insert().values(id=32,batch_id=31,code_string='private-redemption',status='sold',bought_by_user_id=7,bought_at=now,redeemed_at=now,redeemed_by_admin_id=7,redemption_note='fulfilled'))
            c.execute(md.tables['redemption_transaction'].insert().values(id=33,user_id=7,code_id=32,batch_id=31,partner_id=30,batch_name_snapshot='batch',partner_name_snapshot='partner',amount=2,timestamp=now))
            c.execute(md.tables['danmuku_exchange'].insert().values(id=34,user_id=7,qq_user_id='private',room_id='private',yuan=1,huo=1,amount=2,code_string='private-danmuku',timestamp=now))
            waters = reset._highwaters(c,md)
            waters.update(market=700,audit_event=99)
            reset._restore_watermarks(c,md,waters)
        await conn.run_sync(write)
    await engine.dispose()


async def flow(source,target):
    await seed(source)
    manifest = await reset.export_database(source,'123.456789')
    result = await reset.import_database(source,target,manifest)
    assert result['humans'] == result['dependency_accounts'] == 1
    await reset.verify_database(target,manifest)
    engine = create_async_engine(target)
    async with engine.connect() as c:
        rows = (await c.execute(text('SELECT id,cash,debt,economic_version,is_active,is_superuser,equipped_title_id FROM "user" ORDER BY id'))).all()
        assert [r.id for r in rows] == [7,9]
        assert Decimal(str(rows[0].cash)) == Decimal('123.456789')
        assert rows[0].economic_version == 20 and not rows[0].is_active and rows[0].is_superuser and rows[0].equipped_title_id == 4
        assert rows[1].cash == rows[1].debt == 0 and not rows[1].is_active and rows[1].equipped_title_id is None
        assert (await c.execute(text('SELECT MIN(id) FROM audit_event'))).scalar_one() == 100
        for name in ('redemption_partner','redemption_batch','redemption_code','redemption_transaction','danmuku_exchange'):
            assert (await c.execute(text(f'SELECT COUNT(*) FROM {name}'))).scalar_one() == 0
        # An empty business allocator still remembers IDs consumed before reset.
        def allocator(c):
            return reset._highwaters(c, reset._metadata(c))['market']
        assert await c.run_sync(allocator) == 700
    async with async_sessionmaker(engine)() as s:
        events = await audit_replay.load_events(s)
        snap,mismatch = audit_replay.fold(events,check=True)
        assert not mismatch and not await audit_replay.compare_with_live(s,snap)
    await engine.dispose()
    return manifest


@pytest.mark.asyncio
async def test_sqlite_reset_flow_and_refusals(tmp_path):
    source,target = [f'sqlite+aiosqlite:///{tmp_path}/{n}.db' for n in ('source','target')]
    manifest = await flow(source,target)
    with pytest.raises(ValueError,match='must differ'):
        await reset.import_database(source,source,manifest)
    with pytest.raises(ValueError,match='not empty'):
        await reset.import_database(source,target,manifest)
    view_target = f'sqlite+aiosqlite:///{tmp_path}/view.db'
    view_engine = create_async_engine(view_target)
    async with view_engine.begin() as c:
        await c.execute(text('CREATE VIEW old_business_view AS SELECT 1 AS old_data'))
    with pytest.raises(ValueError,match='not empty'):
        await reset.import_database(source,view_target,manifest)
    async with view_engine.connect() as c:
        assert await c.run_sync(lambda conn: inspect(conn).get_view_names()) == ['old_business_view']
        assert await c.run_sync(lambda conn: inspect(conn).get_table_names()) == []
    await view_engine.dispose()
    # Empty retained tables still require the exact fields and foreign keys.
    target_engine = create_async_engine(target)
    async with target_engine.begin() as c:
        await c.execute(text('DELETE FROM title_code'))
        await c.execute(text('DELETE FROM user_title'))
        await c.execute(text('DELETE FROM title_code_batch'))
        await c.execute(text('UPDATE "user" SET equipped_title_id=NULL'))
        await c.execute(text('DELETE FROM title'))
        await c.execute(text('ALTER TABLE title ADD COLUMN unknown_retained_field INTEGER'))
    with pytest.raises(ValueError,match='retained fields'):
        await reset.verify_database(target,manifest)
    async with target_engine.begin() as c:
        await c.execute(text('ALTER TABLE title DROP COLUMN unknown_retained_field'))
        await c.execute(text('DROP TABLE title_code'))
        await c.execute(text('CREATE TABLE title_code (id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL, code_string VARCHAR NOT NULL UNIQUE, status VARCHAR NOT NULL, used_by_user_id INTEGER, used_at DATETIME)'))
    with pytest.raises(ValueError,match='retained foreign key'):
        await reset.verify_database(target,manifest)
    await target_engine.dispose()
    with pytest.raises(ValueError,match='balance changed'):
        await reset.export_database(source,'100')
    private = tmp_path / 'private'
    path = reset.save_manifest(private,manifest)
    assert path.stat().st_mode & 0o777 == 0o600 and private.stat().st_mode & 0o777 == 0o700
    assert reset.load_manifest(private) == manifest
    with pytest.raises(FileExistsError):
        reset.save_manifest(private,manifest)
    assert 'private-code' not in str(reset.summary(manifest)) and 'private-7' not in str(reset.summary(manifest))
    engine = create_async_engine(source)
    async with engine.begin() as c:
        await c.execute(text('UPDATE "user" SET economic_version=2147483647 WHERE id=7'))
    with pytest.raises(ValueError,match='economic version'):
        await reset.export_database(source)
    async with engine.begin() as c:
        await c.execute(text('UPDATE "user" SET economic_version=19 WHERE id=7'))
        await c.execute(text('PRAGMA ignore_check_constraints=ON'))
        await c.execute(text('UPDATE "user" SET cash=-1 WHERE id=7'))
    with pytest.raises(ValueError,match='account amount'):
        await reset.export_database(source)
    async with engine.begin() as c:
        await c.execute(text('UPDATE "user" SET cash=55 WHERE id=7'))
        await c.execute(text("UPDATE siteconfig SET value='124' WHERE key='initial_balance'"))
    with pytest.raises(ValueError,match='balance changed'):
        await reset.import_database(source,target,manifest)
    async with engine.begin() as c:
        await c.execute(text('UPDATE title_code SET used_by_user_id=999'))
    with pytest.raises(ValueError,match='dependency identity'):
        await reset.export_database(source)
    async with engine.begin() as c:
        await c.execute(text('UPDATE title_code SET used_by_user_id=9'))
        await c.execute(text('ALTER TABLE title ADD COLUMN unexpected INTEGER'))
    with pytest.raises(ValueError,match='retained fields'):
        await reset.export_database(source)
    await engine.dispose()


@pytest.mark.asyncio
async def test_sqlite_import_failure_rolls_back(tmp_path,monkeypatch):
    source,target = [f'sqlite+aiosqlite:///{tmp_path}/{n}.db' for n in ('source','target')]
    await seed(source)
    manifest = await reset.export_database(source)
    def fail(*args):
        raise RuntimeError('interrupted')
    monkeypatch.setattr(reset,'_verify',fail)
    with pytest.raises(RuntimeError,match='interrupted'):
        await reset.import_database(source,target,manifest)
    engine = create_async_engine(target)
    async with engine.connect() as c:
        assert await c.run_sync(lambda conn: inspect(conn).get_table_names()) == []
    await engine.dispose()


@pytest.mark.pg
@pytest.mark.asyncio
async def test_postgres_reset_flow():
    source,target = [os.environ.get(k) for k in ('RESET_TEST_PG_SOURCE','RESET_TEST_PG_TARGET')]
    if not source or not target:
        pytest.skip('explicit isolated source and target required')
    from sqlalchemy.engine import make_url
    assert all(make_url(u).database.endswith('_test') for u in (source,target))
    await flow(source,target)


def test_production_urls_use_application_settings_resolver(monkeypatch):
    from app.core import config
    from sqlalchemy.engine import make_url

    settings = config.Settings(
        _env_file=None, DB_BACKEND='postgres', PG_HOST='postgres', PG_PORT=5432,
        PG_USER='thccb', PG_DB='thccb', DATABASE_URL=None,
        PG_PASSWORD='synthetic-reset-test-password',
        SECRET_KEY='synthetic-reset-test-secret-key-32-chars',
        ADMIN_SECRET_KEY='synthetic-admin-test-secret-key-32-chars',
        CASDOOR_ENDPOINT='', CASDOOR_CLIENT_ID='', CASDOOR_CLIENT_SECRET='',
        APP_ENV='development',
    )
    monkeypatch.setattr(config, 'settings', settings)
    source, target, before = reset.production_urls('20261013')
    assert source == make_url(settings.build_db_url())
    assert target.database == 'thccb_reset_20261013'
    assert before.database == 'thccb_before_20261013'

    # Explicit DATABASE_URL keeps the application's highest-priority override.
    settings.PG_HOST = 'different-host'
    settings.PG_DB = 'different-db'
    settings.DATABASE_URL = 'postgresql+asyncpg://override:synthetic@postgres:5432/thccb'
    source, _, _ = reset.production_urls('20261013')
    assert source == make_url(settings.DATABASE_URL)
    settings.DATABASE_URL = 'postgresql+asyncpg://override:synthetic@different-host:5432/thccb'
    with pytest.raises(ValueError, match='fixed Compose PostgreSQL'):
        reset.production_urls('20261013')
