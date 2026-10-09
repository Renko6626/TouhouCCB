import importlib.util
from pathlib import Path
from decimal import Decimal
import pytest
from sqlalchemy import select, text, inspect
spec = importlib.util.spec_from_file_location('rebuild', Path(__file__).parents[1] / 'scripts/rebuild_user_database.py')
r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)

@pytest.fixture
def source(tmp_path):
    url = 'sqlite:///' + str(tmp_path / 'source.db'); m = r.metadata(); m.tables['market'].dialect_options['sqlite']['autoincrement'] = True; e = r.engine(url); m.create_all(e)
    with e.begin() as c:
        c.execute(m.tables['title'].insert().values(id=7,name='owned'))
        c.execute(m.tables['user'].insert().values(id=42,username='disabled',casdoor_id='sso',is_active=False,is_superuser=True,cash=90,debt=20,credit_frozen=True,economic_version=8,equipped_title_id=7))
        c.execute(m.tables['user'].insert().values(id=43,username='bot',is_bot=True,cash=999))
        c.execute(m.tables['user_title'].insert().values(id=3,user_id=42,title_id=7,source='admin',granted_by_admin_id=43))
        c.execute(m.tables['title_code_batch'].insert().values(id=4,title_id=7,name='claimed',created_by_admin_id=43))
        c.execute(m.tables['title_code'].insert().values(id=5,batch_id=4,code_string='used-title',status='used',used_by_user_id=42))
        c.execute(m.tables['danmuku_exchange'].insert().values(id=6,user_id=42,qq_user_id='001',room_id='test',yuan=2,huo=3,amount=5,code_string='signed-code'))
        c.execute(m.tables['redemption_partner'].insert().values(id=9,name='partner'))
        c.execute(m.tables['redemption_batch'].insert().values(id=10,partner_id=9,name='batch',unit_price=5,created_by_admin_id=43))
        c.execute(m.tables['redemption_code'].insert().values(id=11,batch_id=10,code_string='secret',status='sold',bought_by_user_id=42,redeemed_by_admin_id=43,redemption_note='fulfilled'))
        c.execute(m.tables['redemption_transaction'].insert().values(id=12,user_id=42,code_id=11,batch_id=10,partner_id=9,amount=5))
        c.execute(m.tables['audit_event'].insert().values(id=1,event_type='redeem_fulfill',user_id=42,operator_user_id=43,ref_table='redemption_code',ref_id=11,payload={'note':'fulfilled'},user_after={'cash':'90','debt':'20','debt_last_accrued_at':None}))
        c.execute(m.tables['market'].insert().values(id=100,title='old'))
        c.execute(m.tables['outcome'].insert().values(id=200,market_id=100,label='old'))
        c.execute(m.tables['position'].insert().values(user_id=42,outcome_id=200,amount=4))
        c.execute(m.tables['market'].insert().values(id=500,title='deleted'))
        c.execute(m.tables['market'].delete().where(m.tables['market'].c.id == 500))
    e.dispose(); return url

def test_identity_rights_reset_and_ids(source,tmp_path):
    target = 'sqlite:///' + str(tmp_path / 'target.db'); data = r.export_data(source,{'balance':'1000','dependency_balance':'0','sequence_highwater':{'outcome':200,'fx_pair':0}})
    assert data['dependencies'] == [43]
    export_file = tmp_path / 'private.json'; r.write_private(export_file,data)
    assert export_file.stat().st_mode & 0o777 == 0o600
    r.import_data(source,target,data); assert r.verify(target,data)['verified_users'] == 2
    e = r.engine(target); m = r.metadata()
    with e.begin() as c:
        u = c.execute(select(m.tables['user']).where(m.tables['user'].c.id == 42)).mappings().one()
        assert (u['casdoor_id'],u['is_active'],u['is_superuser'],u['cash'],u['debt'],u['credit_frozen']) == ('sso',False,True,Decimal('1000'),0,False)
        from app.models.audit import AuditEvent
        from app.services.audit_replay import fold
        events = [AuditEvent(**dict(x)) for x in c.execute(select(m.tables['audit_event']).order_by(m.tables['audit_event'].c.id)).mappings()]
        snapshot, mismatches = fold(events,check=True)
        assert mismatches == []
        assert snapshot.users[42].cash == Decimal('1000')
        assert c.execute(m.tables['market'].insert().values(title='new')).inserted_primary_key[0] > 500
    e.dispose()
    with pytest.raises(ValueError,match='empty'): r.import_data(source,target,data)
    with pytest.raises(ValueError,match='differ'): r.import_data(source,source,data)

def test_invalid_payload_and_transaction_rollback(source,tmp_path):
    target = 'sqlite:///' + str(tmp_path / 'target.db'); data = r.export_data(source,{'balance':'1000','dependency_balance':'0','sequence_highwater':{'outcome':200,'fx_pair':0}})
    data['rows']['user'][0]['debt'] = '1'
    with pytest.raises(ValueError,match='fields'): r.import_data(source,target,data)
    e = r.engine(target); assert inspect(e).get_table_names() == []
    del data['rows']['user'][0]['debt']; data['rows']['user'][1]['username'] = data['rows']['user'][0]['username']
    with pytest.raises(Exception): r.import_data(source,target,data)
    with e.connect() as c: assert c.execute(text('SELECT count(*) FROM "user"')).scalar() == 0
    e.dispose()


def test_missing_disabled_account_state_rejected_before_target_mutation(source,tmp_path):
    target_path = tmp_path / 'damaged-target.db'
    target = 'sqlite:///' + str(target_path)
    data = r.export_data(source,{'balance':'1000','dependency_balance':'0','sequence_highwater':{'outcome':200,'fx_pair':0}})
    del data['rows']['user'][0]['is_active']
    with pytest.raises(ValueError,match='incomplete identity fields'):
        r.import_data(source,target,data)
    assert not target_path.exists()


def test_large_excluded_history_is_counted_without_materializing_rows(source, monkeypatch):
    from sqlalchemy import Column, ForeignKey, Integer, MetaData, Table
    from sqlalchemy.engine import CursorResult

    e = r.engine(source)
    m = r.metadata()
    with e.begin() as c:
        c.execute(m.tables['outcome'].insert(), [dict(id=i,market_id=100,label=str(i)) for i in range(201,2201)])
        c.execute(m.tables['position'].insert(), [dict(user_id=42,outcome_id=i,amount=1) for i in range(201,2201)])
        c.execute(m.tables['audit_event'].insert(), [dict(event_type='trade',user_id=42,payload={'history':'x' * 1000}) for _ in range(2000)])
        reflected = MetaData(); reflected.reflect(c)
        unknown = Table('unknown_user_history', reflected, Column('id',Integer,primary_key=True), Column('user_id',Integer,ForeignKey('user.id')))
        unknown.create(c)
        c.execute(unknown.insert(), [dict(id=i,user_id=42) for i in range(1,2001)])
    e.dispose()

    # Count rows actually exposed to Python as full mappings, including audit payloads.
    materialized = []
    original = CursorResult.mappings
    def mappings(result):
        for row in original(result):
            materialized.append(dict(row))
            yield row
    monkeypatch.setattr(CursorResult, 'mappings', mappings)
    data = r.export_data(source,{'balance':'1000','dependency_balance':'0','sequence_highwater':{'outcome':200,'fx_pair':0}})
    assert data['excluded_counts']['position'] == 2001
    assert data['excluded_counts']['unknown_user_history'] == 2000
    assert data['source_summary']['position_rows'] == 2001
    assert data['unsupported'] == ['unknown_user_history']
    assert data['highwater']['unknown_user_history'] == 2000
    assert data['highwater']['audit_event'] == 2001
    assert [row['id'] for row in data['rows']['audit_event']] == [1]
    assert len(materialized) < 50
    assert all(row.get('event_type') != 'trade' for row in materialized)
