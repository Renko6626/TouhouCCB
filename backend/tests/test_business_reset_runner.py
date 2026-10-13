"""Observable maintenance failure boundaries; no financial logic simulation."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture
def setup_db():
    yield  # Host runner tests do not access the shared application database.


FAKE = r'''
import json, os, sys
from pathlib import Path
p=Path(os.environ['STATE']); s=json.loads(p.read_text()); a=sys.argv[1:]
f=os.environ.get('FAIL',''); cmd=Path(sys.argv[0]).name

def done(event):
    s['events'].append(event); p.write_text(json.dumps(s))
    if event==f: sys.exit(1)

if cmd=='sleep': sys.exit(0)
if cmd=='sudo': done('cache-clear' if '-name' not in a and a[1]=='find' else 'cache-preflight'); sys.exit(0)
if cmd=='curl':
    if f=='owner-health' and s['owner']: sys.exit(1)
    assert s['running']; done('health'); sys.exit(0)
if a[0]=='ps': sys.exit(0)
if a[0]=='image': print('isolated immutable old image'); sys.exit(0)
if a[0]=='inspect':
    if a[-1]=='thccb-postgres': print('network')
    elif '.Config.Env' in a[1]: print('APP_BUILD_SHA='+os.environ['EXPECTED_BUILD_SHA'])
    else: print('old-container old-image')
    sys.exit(0)
if a[0]=='network': print('thccb-postgres\nthccb-backend'); sys.exit(0)
assert a[0]=='compose',a
# Strip flags and compose files, leaving the actual subcommand.
a=a[1:]
while a[0] in ('--project-directory','--env-file','-f'): a=a[2:]
if a[0]=='pull': done('pull')
elif a[0]=='ps':
    if s['running']: print('backend')
elif a[0] in ('start','stop','up'):
    if a[0]=='stop': s['running']=False; done('stop')
    elif a[0]=='start': s['running']=True; done('restart-old')
    else:
        override=Path(os.environ['ROOT'])/'backups/business-reset-123/compose.override.yml'
        s['owner']='"false"' in override.read_text(); s['running']=True
        done('owner-start' if s['owner'] else 'readonly-start')
elif a[0]=='run':
    if 'business_reset.py' in ' '.join(a):
        i=a.index('scripts/business_reset.py'); stage=a[i+1]
        if stage=='import': assert s['restored'] and not s['running']
        if stage=='verify' and '--after-cutover' in a: assert s['readonly'] and not s['owner']
        done(stage)
    elif 'scripts/audit_verify.py' in a:
        assert s['readonly'] and not s['owner']; done('audit')
    elif '-c' in a: print('"thccb"')
    else: raise AssertionError(a)
elif a[:3]==['exec','-T','postgres']:
    a=a[3:]
    if a[0]=='pg_dump': assert not s['running']; done('dump'); print('PGDMP isolated dump')
    elif a[0]=='pg_restore': assert sys.stdin.read(); s['restored']=True; done('restore')
    elif a[0]=='createdb': s['databases'].append(a[-1]); done('create')
    elif a[0]=='dropdb': s['databases'].remove(a[-1]); done('drop')
    elif a[0]=='psql':
        sql=a[-1]
        if 'SELECT count(*)' in sql: print('0')
        elif 'SELECT CASE' in sql: print('ALTER ROLE thccb IN DATABASE thccb RESET default_transaction_read_only;')
        elif 'SET default_transaction_read_only TO on' in sql: s['readonly']=True; done('protect')
        elif 'RESET default_transaction_read_only' in sql: s['readonly']=False; done('unprotect')
        elif 'RENAME TO' in sql:
            old='thccb' if 'ALTER DATABASE thccb RENAME' in sql else 'thccb_reset_123'
            new='thccb_before_123' if old=='thccb' else 'thccb'
            # Failure occurs before the second rename, preserving both DBs.
            done('rename-first' if old=='thccb' else 'rename-second')
            s['databases'].remove(old); s['databases'].append(new)
        else: done('connections')
    else: raise AssertionError(a)
else: raise AssertionError(a)
p.write_text(json.dumps(s))
'''


@pytest.fixture
def runner(tmp_path):
    root=tmp_path/'project'; stage=root/'.business-reset-123'
    for path in [stage/'deploy', stage/'dist', root/'deploy', root/'thccb-frontend/dist']:
        path.mkdir(parents=True)
    source=Path(__file__).resolve().parents[2]
    shell=(source/'deploy/business_reset.sh').read_text().replace('ROOT=/home/deploy/TouhouCCB',f'ROOT={root}')
    (stage/'deploy/business_reset.sh').write_text(shell)
    (stage/'deploy/nginx.conf').write_text((source/'deploy/nginx.conf').read_text())
    for path in [root/'docker-compose.yml',stage/'docker-compose.yml',root/'.env',stage/'dist/index.html']:
        path.write_text('isolated fixture')
    commands=tmp_path/'bin'; commands.mkdir()
    for name in ('docker','sudo','curl','sleep'):
        p=commands/name; p.write_text(f'#!{sys.executable}\n'+FAKE); p.chmod(0o755)
    def run(action='execute',failure=''):
        state=tmp_path/'state.json'; state.write_text(json.dumps({'running':True,'owner':False,'readonly':False,'restored':False,'databases':['thccb'],'events':[]}))
        env={**os.environ,'PATH':f'{commands}:{os.environ["PATH"]}','STATE':str(state),'ROOT':str(root),'FAIL':failure,
             'RESET_RUN_ID':'123','RESET_ACTION':action,'RESET_CONFIRMATION':'RESET','DATABASE_RESET_PENDING':'true',
             'EXPECTED_INITIAL_BALANCE':'25000','EXPECTED_BUILD_SHA':'a'*40,'BACKEND_IMAGE':'registry/image:'+('a'*40)}
        result=subprocess.run(['bash',str(stage/'deploy/business_reset.sh')],env=env,capture_output=True,text=True,timeout=15)
        return result,json.loads(state.read_text()),root
    return run


def test_preview_preserves_source_and_service(runner):
    result,state,_=runner('preview')
    assert result.returncode==0,result.stderr
    assert state['running'] and state['databases']==['thccb']
    assert not state['restored'] and 'stop' not in state['events']


def test_success_accepts_readonly_then_starts_owner_before_deleting_old(runner):
    result,state,root=runner()
    assert result.returncode==0,result.stderr
    assert state['databases']==['thccb'] and state['running'] and state['owner'] and not state['readonly']
    assert state['events'].index('audit')<state['events'].index('unprotect')<state['events'].index('owner-start')<len(state['events'])-1
    backup=root/'backups/business-reset-123'
    assert (backup/'source.dump').stat().st_mode&0o777==0o600
    assert (backup/'stage').read_text()=='complete\n'


@pytest.mark.parametrize('failure',['restore','import'])
def test_before_rename_failure_restores_previous_service(runner,failure):
    result,state,_=runner(failure=failure)
    assert result.returncode!=0 and state['running'] and not state['owner']
    assert 'thccb' in state['databases'] and 'restart-old' in state['events']


@pytest.mark.parametrize('failure',['rename-second','audit'])
def test_cutover_failure_preserves_both_databases_and_stops_backend(runner,failure):
    result,state,_=runner(failure=failure)
    assert result.returncode!=0 and not state['running']
    assert len(state['databases'])==2 and 'thccb_before_123' in state['databases']
    assert 'restart-old' not in state['events'] and 'owner-start' not in state['events']


def test_owner_health_failure_never_rolls_back_or_deletes_old(runner):
    result,state,_=runner(failure='owner-health')
    assert result.returncode!=0 and state['owner'] and not state['running']
    assert set(state['databases'])=={'thccb','thccb_before_123'}
    assert 'restart-old' not in state['events']


def test_cache_permission_failure_leaves_source_running(runner):
    result,state,_=runner(failure='cache-preflight')
    assert result.returncode!=0 and state['running'] and state['databases']==['thccb']
    assert 'stop' not in state['events']
