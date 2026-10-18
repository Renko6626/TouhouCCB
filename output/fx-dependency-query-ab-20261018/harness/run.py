import os,sys,subprocess,time,json,urllib.request,shutil,hashlib
from pathlib import Path
root=Path(__file__).resolve().parent;output=Path('/data/sunyunbo/www/TouhouCCB/output/fx-dependency-query-ab-20261018');output.mkdir(exist_ok=True)
base=os.environ.copy();base.update(APP_ENV='development',SECRET_KEY='local-financing-mixed-only-20261017',ADMIN_SECRET_KEY='local-financing-mixed-admin-only-20261017',CASDOOR_ENDPOINT='',CASDOOR_CLIENT_ID='',CASDOOR_CLIENT_SECRET='',DB_POOL_SIZE='10',DB_MAX_OVERFLOW='20')
def run(cmd,env,log,cwd):
    with open(log,'a') as f:return subprocess.run(cmd,env=env,cwd=cwd,stdout=f,stderr=subprocess.STDOUT).returncode
for variant in ('before','after'):
    source=root/variant;directory=output/variant;directory.mkdir();private_run=source/'run';private_run.mkdir()
    dbname=f'fx_dependency_query_{variant}_test';env=base|{'DATABASE_URL':f'postgresql+asyncpg://sunyunbo@/{dbname}?host=/tmp/sunyunbo/fx-pool-incident-20261017/socket&port=55489','LOCAL_USERS':'100','LOCAL_DB_NAME':dbname,'LOCAL_RUN_DIRECTORY':str(private_run),'PYTHONPATH':str(source/'backend')+':'+str(source)+':'+str(root)}
    names=('app/api/v1/user_trading.py','app/services/credit/trading_context.py','app/services/fx/net_trading.py','app/services/loan_service.py')
    (directory/'code-sha256.json').write_text(json.dumps({n:hashlib.sha256((source/'backend'/n).read_bytes()).hexdigest() for n in names},indent=2)+'\n')
    subprocess.run(['/usr/lib/postgresql/14/bin/createdb','-h','/tmp/sunyunbo/fx-pool-incident-20261017/socket','-p','55489',dbname],check=True)
    print(variant+': prepare',flush=True)
    for script in ('migrate.py','seed.py'):
        if run([sys.executable,str(source/script)],env,directory/'setup.log',source/'backend'):raise RuntimeError('setup failed')
    serverlog=open(directory/'backend.log','w');server=subprocess.Popen([sys.executable,'-m','uvicorn','count_server:app','--host','127.0.0.1','--port','18543','--no-access-log'],env=env,cwd=source,stdout=serverlog,stderr=subprocess.STDOUT)
    try:
        for _ in range(100):
            if server.poll() is not None:raise RuntimeError('backend exited')
            try:
                with urllib.request.urlopen('http://127.0.0.1:18543/api/v1/fx/pairs',timeout=1) as r:
                    if r.status==200:break
            except Exception:time.sleep(.2)
        else:raise RuntimeError('backend not ready')
        print(variant+': deterministic reads',flush=True)
        if run([sys.executable,str(root/'read_driver.py')],env,directory/'read-driver.log',source):raise RuntimeError('deterministic reads failed')
        print(variant+': 100 users, 200 SSE mixed scenario',flush=True)
        args=[sys.executable,str(root/'compare_driver.py'),'--base-url','http://127.0.0.1:18543','--tokens',str(private_run/'private/tokens.json'),'--mode','full','--users','100','--smoke-seconds','10','--min-interval','1','--max-interval','1.5','--join-interval','.3','--readonly-seconds','30','--normal-seconds','60','--active-seconds','45','--stress-seconds','30','--burst-observe-seconds','10','--orders-seconds','0','--output',str(directory/'http')]
        status=run(args,env,directory/'driver.log',source)
        summary=json.loads((directory/'http/summary.json').read_text());print(variant+': finished, driver status='+str(status)+', abort='+str(summary['abort_reason']),flush=True)
    finally:
        server.terminate()
        try:server.wait(timeout=30)
        except subprocess.TimeoutExpired:server.kill();server.wait()
        serverlog.close()
        for name in ('resources.jsonl','sql-counts.json','gates.json','gate-queue.jsonl','phases.jsonl','dependency-sql.json','deterministic.json'):
            file=private_run/name
            if file.exists():shutil.copyfile(file,directory/name)
        (private_run/'private/tokens.json').unlink(missing_ok=True)
print('comparison finished',flush=True)
