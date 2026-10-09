"""Offline user/entitlement rebuild. Connections are explicit; never uses init_db."""
from __future__ import annotations
import argparse
import importlib
import json
import os
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import Integer, MetaData, create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlmodel import SQLModel
from alembic.config import Config
from alembic.script import ScriptDirectory

IDENTITY = ('id', 'casdoor_id', 'username', 'email', 'is_active', 'is_superuser', 'is_bot', 'tos_accepted_at', 'equipped_title_id')
RIGHTS = ('title', 'user_title', 'title_code_batch', 'title_code', 'redemption_partner', 'redemption_batch', 'redemption_code', 'redemption_transaction', 'danmuku_exchange', 'audit_event')
GATES = ('fx_enabled', 'fx_short_enabled', 'loan_enabled', 'liquidation_enabled', 'unified_credit_enabled', 'pve_enabled')
REMOVED = {'fx_hourly_sigma','fx_step_max_ratio','fx_noise_interval_sec','fx_noise_pool_ratio','fx_system_half_life_sec','fx_default_price_move_limit','fx_daily_budget'}


def engine(url):
    url = make_url(url)
    if url.drivername.startswith('postgresql'):
        url = url.set(drivername='postgresql+psycopg2')
    elif url.drivername.startswith('sqlite'):
        url = url.set(drivername='sqlite')
    return create_engine(url)


def canonical(url):
    u = make_url(url)
    if u.get_backend_name() == 'sqlite':
        return ('sqlite', str(Path(u.database).resolve()))
    return (u.get_backend_name(), u.host or 'localhost', u.port or 5432, u.database)


def metadata():
    for module in ('base','redemption','title','ledger','audit','bot','fx','credit'):
        importlib.import_module('app.models.' + module)
    return SQLModel.metadata


def encode(value):
    if isinstance(value, (Decimal, datetime)):
        return str(value) if isinstance(value, Decimal) else value.isoformat()
    raise TypeError(type(value).__name__)


def balance(policy, key='balance'):
    value = Decimal(str(policy[key]))
    if not value.is_finite() or value < 0 or value > Decimal('9999999999.999999') or value.as_tuple().exponent < -6:
        raise ValueError('invalid explicit balance policy')
    return value


def export_data(source_url, policy):
    balance(policy)
    dependency_balance = balance(policy, 'dependency_balance')
    e = engine(source_url)
    try:
        with e.connect() as c:
            if c.dialect.name == 'postgresql':
                c = c.execution_options(isolation_level='REPEATABLE READ')
                c.execute(text('SET TRANSACTION READ ONLY'))
            elif c.dialect.name == 'sqlite':
                c.exec_driver_sql('PRAGMA query_only=ON')
                c.exec_driver_sql('BEGIN')
            else:
                raise ValueError('unsupported database dialect')
            m = MetaData(); m.reflect(c)
            rows = {n: [dict(r) for r in c.execute(select(t)).mappings()] for n,t in m.tables.items() if n != 'alembic_version'}
            all_users = {r['id']: r for r in rows['user']}
            real_ids = {r['id'] for r in rows['user'] if not r.get('is_bot', False)}
            if 'user_ids' in policy:
                real_ids = set(policy['user_ids'])
                if not real_ids <= set(all_users) or any(all_users[i].get('is_bot') for i in real_ids):
                    raise ValueError('user scope contains absent or bot users')
            chosen = {n: {} for n in RIGHTS}
            for n,field in (('user_title','user_id'),('title_code','used_by_user_id'),('redemption_code','bought_by_user_id'),('redemption_transaction','user_id'),('danmuku_exchange','user_id')):
                for r in rows.get(n, []):
                    if r.get(field) in real_ids:
                        chosen[n][r['id']] = r
            for r in rows.get('audit_event', []):
                if r.get('event_type') in {'redeem_purchase','redeem_fulfill','redeem_fulfill_revoke','danmuku_exchange'} and r.get('user_id') in real_ids:
                    if r.get('market_id') is not None or r.get('outcome_id') is not None:
                        raise ValueError('unsupported entitlement audit market reference')
                    dest = r.get('ref_table'); ref = r.get('ref_id')
                    if dest is not None and (dest not in chosen or ref not in chosen[dest]):
                        raise ValueError('unsupported entitlement audit reference')
                    chosen['audit_event'][r['id']] = r
            users = set(real_ids)
            for r in chosen['audit_event'].values():
                for field in ('user_id','operator_user_id'):
                    if r.get(field) is not None:
                        if r[field] not in all_users: raise ValueError('missing audit user dependency')
                        users.add(r[field])
            changed = True
            while changed:
                before = sum(map(len, chosen.values())) + len(users)
                for uid in list(real_ids):
                    tid = all_users[uid].get('equipped_title_id')
                    if tid is not None:
                        matches = [r for r in rows.get('title',[]) if r['id'] == tid]
                        if not matches: raise ValueError('dangling equipped title')
                        chosen['title'][tid] = matches[0]
                for n,items in chosen.items():
                    for r in list(items.values()):
                        for fk in m.tables[n].foreign_keys:
                            v = r.get(fk.parent.name)
                            if v is None: continue
                            dest = fk.column.table.name
                            if dest == 'user':
                                if v not in all_users: raise ValueError('dangling user dependency')
                                users.add(v)
                            elif dest in chosen:
                                matches = [x for x in rows[dest] if x[fk.column.name] == v]
                                if not matches: raise ValueError('dangling entitlement dependency')
                                for x in matches: chosen[dest][x['id']] = x
                            else: raise ValueError('unsupported entitlement reference')
                changed = before != sum(map(len, chosen.values())) + len(users)
            # Unknown user-linked tables require an explicit exclusion decision.
            known_excluded = {'position','transaction','siteconfig','market','liquidation_events','bot_suspicion','market_required_title','audit_event','ledger_entry','fx_wallet','fx_short_position','fx_trade','liquidation_run','liquidation_action','bot','bot_config','bot_profile','fx_event'}
            unsupported = []
            for n,t in m.tables.items():
                if n in RIGHTS or n == 'user' or n in known_excluded: continue
                for fk in t.foreign_keys:
                    dest = fk.column.table.name
                    retained_ids = users if dest == 'user' else set(chosen.get(dest, {}))
                    if retained_ids and any(r.get(fk.parent.name) in retained_ids for r in rows.get(n, [])):
                        if n not in policy.get('exclude_tables', []): unsupported.append(n)
            highwater = {}
            uncertain = []
            for n,t in m.tables.items():
                if 'id' not in t.c or not isinstance(t.c.id.type, Integer): continue
                high = max((r['id'] for r in rows.get(n,[]) if r['id'] is not None), default=0)
                if c.dialect.name == 'postgresql':
                    seq = c.execute(text('SELECT pg_get_serial_sequence(:t, :col)'), {'t': n, 'col':'id'}).scalar()
                    if seq:
                        high = max(high, c.exec_driver_sql('SELECT last_value FROM ' + seq).scalar())
                elif c.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'")).scalar():
                    high = max(high, c.execute(text('SELECT seq FROM sqlite_sequence WHERE name=:n'), {'n':n}).scalar() or 0)
                if c.dialect.name == 'sqlite' and n in {'market','outcome','fx_pair'}:
                    ddl = c.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:n"), {'n':n}).scalar() or ''
                    if 'AUTOINCREMENT' not in ddl.upper() and n not in policy.get('sequence_highwater', {}): uncertain.append(n)
                override = policy.get('sequence_highwater', {}).get(n, 0)
                if not isinstance(override,int) or override < 0: raise ValueError('invalid policy sequence watermark')
                highwater[n] = max(high,override)
            user_rows = []
            for uid in sorted(users):
                r = {k:all_users[uid].get(k) for k in IDENTITY}
                if uid not in real_ids:
                    r['is_active'] = False
                    r['equipped_title_id'] = None
                r['cash'] = str(balance(policy) if uid in real_ids else dependency_balance)
                user_rows.append(r)
            return json.loads(json.dumps({'version':1,'uncertain_sequences':uncertain,'policy':policy,'real_ids':sorted(real_ids),'dependencies':sorted(users-real_ids),'unsupported':sorted(set(unsupported)), 'excluded_counts':{n:len(v) for n,v in rows.items() if n not in RIGHTS and n != 'user'}, 'source_summary':{'real_users':len(real_ids),'active_users':sum(bool(all_users[i]['is_active']) for i in real_ids),'superusers':sum(bool(all_users[i]['is_superuser']) for i in real_ids),'cash_total':str(sum(Decimal(str(all_users[i]['cash'])) for i in real_ids)),'debt_total':str(sum(Decimal(str(all_users[i].get('debt',0))) for i in real_ids)),'position_rows':len(rows.get('position',[])),'fx_wallet_rows':len(rows.get('fx_wallet',[])),'fx_short_rows':len(rows.get('fx_short_position',[])),'source_initial_balance':next((x['value'] for x in rows.get('siteconfig',[]) if x['key']=='initial_balance'),None)}, 'rows':{'user':user_rows, **{n:list(v.values()) for n,v in chosen.items()}}, 'highwater':highwater}, default=encode))
    finally:
        e.dispose()


def validate(data):
    if data.get('version') != 1 or data.get('unsupported') or data.get('uncertain_sequences'): raise ValueError('unsupported export manifest')
    balance(data['policy']); balance(data['policy'],'dependency_balance')
    if set(data['rows']) != {'user', *RIGHTS}: raise ValueError('unexpected import tables')
    m = metadata()
    for n,rows in data['rows'].items():
        seen = set()
        for r in rows:
            allowed = set(IDENTITY) | {'cash'} if n == 'user' else set(m.tables[n].c.keys())
            if n == 'user' and set(r) != allowed:
                raise ValueError('incomplete identity fields')
            if n == 'user' and (not isinstance(r['username'],str) or any(type(r[k]) is not bool for k in ('is_active','is_superuser','is_bot'))):
                raise ValueError('invalid required identity values')
            if not set(r) <= allowed or not isinstance(r['id'],int) or r['id'] <= 0 or r['id'] in seen: raise ValueError('invalid row fields or duplicate id')
            seen.add(r['id'])
            for fk in m.tables[n].foreign_keys:
                v = r.get(fk.parent.name)
                if v is not None and not any(x[fk.column.name] == v for x in data['rows'].get(fk.column.table.name, [])):
                    raise ValueError('missing foreign key dependency')
    real = set(data['real_ids']); deps = set(data['dependencies'])
    if real & deps or real | deps != {r['id'] for r in data['rows']['user']}: raise ValueError('invalid user scopes')
    for r in data['rows']['user']:
        expected = balance(data['policy']) if r['id'] in real else balance(data['policy'],'dependency_balance')
        if Decimal(r['cash']) != expected or (r['id'] in deps and r['is_active']): raise ValueError('invalid reset policy')
    for r in data['rows']['audit_event']:
        if r.get('event_type') not in {'redeem_purchase','redeem_fulfill','redeem_fulfill_revoke','danmuku_exchange'}: raise ValueError('unsupported audit event')
        if r.get('market_id') is not None or r.get('outcome_id') is not None: raise ValueError('unsupported audit market reference')
        for field in ('user_id','operator_user_id'):
            if r.get(field) is not None and r[field] not in real | deps: raise ValueError('missing audit user')
        dest = r.get('ref_table'); ref = r.get('ref_id')
        if dest is not None and not any(x['id'] == ref for x in data['rows'].get(dest,[])): raise ValueError('missing audit reference')
    for n,v in data['highwater'].items():
        if not isinstance(v,int) or v < 0: raise ValueError('invalid sequence watermark')


def typed(table, row):
    from sqlalchemy import DateTime, Numeric
    out = dict(row)
    for k,v in out.items():
        if v is not None and isinstance(table.c[k].type, DateTime): out[k] = datetime.fromisoformat(v)
        elif v is not None and isinstance(table.c[k].type, Numeric): out[k] = Decimal(str(v))
    return out


def import_data(source_url, target_url, data):
    if canonical(source_url) == canonical(target_url): raise ValueError('source and target must differ')
    validate(data)
    m = metadata(); e = engine(target_url)
    try:
        with e.begin() as c:
            if inspect(c).get_table_names(): raise ValueError('target must be completely empty; repeat import rejected')
            if c.dialect.name == 'sqlite':
                c.exec_driver_sql('PRAGMA foreign_keys=ON')
                for t in m.tables.values():
                    if len(t.primary_key.columns) == 1 and 'id' in t.c and isinstance(t.c.id.type, Integer):
                        t.dialect_options['sqlite']['autoincrement'] = True
            m.create_all(c)
            cfg = Config(); cfg.set_main_option('script_location', str(Path(__file__).resolve().parents[1] / 'alembic'))
            head = ScriptDirectory.from_config(cfg).get_current_head()
            c.exec_driver_sql('CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)')
            c.execute(text('INSERT INTO alembic_version VALUES (:h)'), {'h':head})
            # Break the user/title dependency cycle without disabling FK checks.
            for r in data['rows']['user']:
                row = typed(m.tables['user'], r); row['equipped_title_id'] = None
                c.execute(m.tables['user'].insert().values(**row))
            for n in ('title','user_title','title_code_batch','title_code','redemption_partner','redemption_batch','redemption_code','redemption_transaction','danmuku_exchange','audit_event'):
                for r in data['rows'][n]: c.execute(m.tables[n].insert().values(**typed(m.tables[n],r)))
            for r in data['rows']['user']:
                c.execute(m.tables['user'].update().where(m.tables['user'].c.id == r['id']).values(equipped_title_id=r['equipped_title_id']))
            from app.services.loan_migrate import DEFAULT_CONFIGS
            defaults = {k:(v,t) for k,v,t in DEFAULT_CONFIGS if k not in REMOVED}
            defaults.update({k:('false','bool') for k in GATES})
            defaults['credit_new_risk_frozen'] = ('true','bool')
            defaults['initial_balance'] = (str(balance(data['policy'])),'decimal')
            defaults['credit_leverage'] = ('2','decimal'); defaults['credit_maintenance_ratio'] = ('0.2','decimal')
            for k,(v,t) in defaults.items(): c.execute(m.tables['siteconfig'].insert().values(key=k,value=v,value_type=t))
            for n,high in data['highwater'].items():
                if n not in m.tables or 'id' not in m.tables[n].c: continue
                actual = c.execute(select(func.max(m.tables[n].c.id))).scalar() or 0
                high = max(high,actual)
                if c.dialect.name == 'postgresql':
                    seq = c.execute(text('SELECT pg_get_serial_sequence(:t, :col)'), {'t':n,'col':'id'}).scalar()
                    if seq and high: c.execute(text('SELECT setval(CAST(:s AS regclass), :v, true)'), {'s':seq,'v':high})
                elif c.dialect.name == 'sqlite' and m.tables[n].dialect_options['sqlite']['autoincrement']:
                    c.execute(text('DELETE FROM sqlite_sequence WHERE name=:n'),{'n':n})
                    c.execute(text('INSERT INTO sqlite_sequence(name,seq) VALUES (:n,:s)'),{'n':n,'s':high})
            for r in data['rows']['user']:
                c.execute(m.tables['audit_event'].insert().values(event_type='user_register',user_id=r['id'],payload={'source':'database_rebuild','initial_balance':r['cash']},user_after={'cash':r['cash'],'debt':'0','debt_last_accrued_at':None}))
    finally: e.dispose()


def verify(target_url, data):
    validate(data); e = engine(target_url); m = metadata()
    try:
        with e.connect() as c:
            for n,expected in data['rows'].items():
                actual = {r['id']:dict(r) for r in c.execute(select(m.tables[n])).mappings()}
                if n == 'audit_event':
                    actual = {i:r for i,r in actual.items() if r['payload'].get('source') != 'database_rebuild'}
                if set(actual) != {r['id'] for r in expected}: raise ValueError('row scope mismatch')
                for r in expected:
                    for k,v in typed(m.tables[n],r).items():
                        got = actual[r['id']][k]
                        if isinstance(v,datetime): got = got.replace(tzinfo=None); v = v.replace(tzinfo=None)
                        if got != v: raise ValueError('preserved field mismatch')
                    if n == 'user' and any(actual[r['id']][k] != v for k,v in {'debt':0,'debt_last_accrued_at':None,'last_liquidated_at':None,'economic_version':0,'credit_frozen':False}.items()): raise ValueError('economic reset mismatch')
            allowed = {'user',*RIGHTS,'siteconfig','audit_event'}
            for n,t in m.tables.items():
                if n not in allowed and c.execute(select(func.count()).select_from(t)).scalar(): raise ValueError('old domain state found')
            if c.dialect.name == 'sqlite' and c.exec_driver_sql('PRAGMA foreign_key_check').fetchall(): raise ValueError('foreign key mismatch')
            anchors = c.execute(select(m.tables['audit_event'])).mappings().all()
            anchors = {r['user_id']:r for r in anchors if r['payload'].get('source') == 'database_rebuild'}
            if set(anchors) != {r['id'] for r in data['rows']['user']}: raise ValueError('missing opening audit anchors')
            for r in data['rows']['user']:
                if Decimal(anchors[r['id']]['user_after']['cash']) != Decimal(r['cash']): raise ValueError('opening audit balance mismatch')
            for n,high in data['highwater'].items():
                if n not in m.tables or 'id' not in m.tables[n].c: continue
                if c.dialect.name == 'postgresql':
                    seq = c.execute(text('SELECT pg_get_serial_sequence(:t, :col)'), {'t':n,'col':'id'}).scalar()
                    if seq and c.exec_driver_sql('SELECT last_value FROM ' + seq).scalar() < high: raise ValueError('sequence below source watermark')
                else:
                    seq = c.execute(text('SELECT seq FROM sqlite_sequence WHERE name=:n'), {'n':n}).scalar() or 0
                    if seq < high: raise ValueError('sequence below source watermark')
            for gate in GATES:
                if c.execute(select(m.tables['siteconfig'].c.value).where(m.tables['siteconfig'].c.key == gate)).scalar() != 'false': raise ValueError('economic gate open')
            return {'verified_users':len(data['rows']['user']), 'verified_entitlements':sum(len(data['rows'][n]) for n in RIGHTS)}
    finally: e.dispose()


def write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd,'w') as f: json.dump(data,f,indent=2,default=encode)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('stage', choices=['inspect','export','import','verify'])
    p.add_argument('--policy',required=True); p.add_argument('--file',required=True)
    args = p.parse_args(); policy = json.loads(Path(args.policy).read_text())
    source = os.environ['REBUILD_SOURCE_DATABASE_URL']
    if args.stage in ('inspect','export'):
        data = export_data(source,policy)
        write_private(args.file,data)
        print(json.dumps({'users':len(data['real_ids']),'dependency_users':len(data['dependencies']),'unsupported_tables':data['unsupported'],'uncertain_sequences':data['uncertain_sequences'],'balance':str(balance(policy)),'source_summary':data['source_summary'],'counts':{n:len(v) for n,v in data['rows'].items()}}))
    else:
        data = json.loads(Path(args.file).read_text())
        if data['policy'] != policy: raise ValueError('policy differs from reviewed export')
        target = os.environ['REBUILD_TARGET_DATABASE_URL']
        if canonical(source) == canonical(target): raise ValueError('source and target must differ')
        if args.stage == 'import': import_data(source,target,data)
        print(json.dumps(verify(target,data)))

if __name__ == '__main__':
    try: main()
    except Exception as exc:
        # Driver exceptions may contain SQL parameters, emails, codes or URLs.
        print('rebuild failed: ' + (str(exc) if isinstance(exc,ValueError) else type(exc).__name__), file=sys.stderr)
        sys.exit(1)
