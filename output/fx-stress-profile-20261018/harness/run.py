import os,sys,subprocess,time,json,urllib.request,shutil
from pathlib import Path
root=Path(__file__).resolve().parent;output=Path('/data/sunyunbo/www/TouhouCCB/output/fx-stress-profile-20261018')
base=os.environ.copy();base.update(APP_ENV='development',SECRET_KEY='local-stress-profile-only-20261018',ADMIN_SECRET_KEY='local-stress-profile-admin-only-20261018',CASDOOR_ENDPOINT='',CASDOOR_CLIENT_ID='',CASDOOR_CLIENT_SECRET='',DB_POOL_SIZE='10',DB_MAX_OVERFLOW='20')
def run(cmd,env,log,cwd):
 with open(log,'a') as f:return subprocess.run(cmd,env=env,cwd=cwd,stdout=f,stderr=subprocess.STDOUT).returncode
commands=[]
for variant in ('control','profile'):
 source=root/variant;directory=output/variant;directory.mkdir();private_run=source/'run';private_run.mkdir()
 dbname=f'fx_stress_profile_{variant}_test';env=base|{'DATABASE_URL':f'postgresql+asyncpg://sunyunbo@/{dbname}?host=/tmp/sunyunbo/fx-pool-incident-20261017/socket&port=55489','LOCAL_USERS':'100','LOCAL_DB_NAME':dbname,'LOCAL_RUN_DIRECTORY':str(private_run),'LOCAL_OUTPUT_DIRECTORY':str(directory),'CPU_PROFILE':'1' if variant=='profile' else '0','PYTHONPATH':str(source/'backend')+':'+str(source)+':'+str(root)}
 subprocess.run(['/usr/lib/postgresql/14/bin/createdb','-h','/tmp/sunyunbo/fx-pool-incident-20261017/socket','-p','55489',dbname],check=True)
 print(variant+': prepare',flush=True)
 for script in ('migrate.py','seed.py'):
  if run([sys.executable,str(source/script)],env,directory/'setup.log',source/'backend'):raise RuntimeError('setup failed')
 subprocess.run(['/usr/lib/postgresql/14/bin/psql','-h','/tmp/sunyunbo/fx-pool-incident-20261017/socket','-p','55489','-d',dbname,'-c','CREATE EXTENSION IF NOT EXISTS pg_stat_statements'],check=True,stdout=subprocess.DEVNULL)
 serverlog=open(directory/'backend.log','w');server=subprocess.Popen([sys.executable,'-m','uvicorn','profile_server:app','--host','127.0.0.1','--port','18543','--no-access-log'],env=env,cwd=source,stdout=serverlog,stderr=subprocess.STDOUT)
 monitorlog=open(directory/'pg-monitor.log','w');monitor=subprocess.Popen([sys.executable,str(root/'pg_monitor.py')],env=env,cwd=source,stdout=monitorlog,stderr=subprocess.STDOUT);native=None;nativelog=None
 try:
  for _ in range(100):
   if server.poll() is not None:raise RuntimeError('backend exited')
   try:
    with urllib.request.urlopen('http://127.0.0.1:18543/api/v1/fx/pairs',timeout=1) as response:
     if response.status==200:break
   except Exception:time.sleep(.2)
  else:raise RuntimeError('backend not ready')
  if variant=='profile':
   nativelog=open(directory/'native-watch.log','w');native=subprocess.Popen([sys.executable,str(root/'native_watch.py')],env=env,cwd=source,stdout=nativelog,stderr=subprocess.STDOUT)
  print(variant+': full scenario (heavy hooks only stress)',flush=True)
  args=[sys.executable,str(root/'compare_driver.py'),'--base-url','http://127.0.0.1:18543','--tokens',str(private_run/'private/tokens.json'),'--mode','full','--users','100','--smoke-seconds','10','--min-interval','1','--max-interval','1.5','--join-interval','.3','--readonly-seconds','30','--normal-seconds','60','--active-seconds','45','--stress-seconds','30','--burst-observe-seconds','10','--orders-seconds','0','--output',str(directory/'http')]
  commands.append(dict(variant=variant,args=[a if a!=str(private_run/'private/tokens.json') else '<deleted-private-tokens>' for a in args],database=dbname));(output/'commands.json').write_text(json.dumps(commands,indent=2)+'\n')
  status=run(args,env,directory/'driver.log',source)
  summary=json.loads((directory/'http/summary.json').read_text());print(variant+': finished status='+str(status)+' abort='+str(summary['abort_reason']),flush=True)
 finally:
  (private_run/'driver-ended').touch()
  if native:
   try:native.wait(timeout=25)
   except subprocess.TimeoutExpired:native.terminate();native.wait(timeout=5)
   nativelog.close()
  # Let independent monitor capture boundary before terminating the service.
  time.sleep(.2);(private_run/'monitor-stop').touch()
  try:monitor.wait(timeout=10)
  except subprocess.TimeoutExpired:monitor.terminate();monitor.wait(timeout=5)
  monitorlog.close();server.terminate()
  try:server.wait(timeout=30)
  except subprocess.TimeoutExpired:server.kill();server.wait()
  serverlog.close()
  for f in private_run.iterdir():
   if f.is_file() and f.suffix in ('.json','.jsonl'):shutil.copyfile(f,directory/f.name)
  (private_run/'private/tokens.json').unlink(missing_ok=True)
print('finished',flush=True)
