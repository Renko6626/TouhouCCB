from pathlib import Path
import shutil,subprocess,hashlib,json
root=Path(__file__).resolve().parent
old=Path('/tmp/sunyunbo/fx-financing-mixed-ab-20261017')
source=Path('/tmp/sunyunbo/thccb-fx-read-checkpoint-20261017')
for name in ('run.py','count_server.py','compare_driver.py','fx_http.py','summarize.py'):
    text=(old/name).read_text().replace('fx-financing-mixed-ab-20261017','fx-dependency-query-ab-20261018').replace('fx_financing_mixed_','fx_dependency_query_')
    (root/name).write_text(text)
for variant in ('before','after'):
    dest=root/variant
    shutil.copytree(source/'backend',dest/'backend',ignore=shutil.ignore_patterns('venv','.env','*.db','__pycache__','.pytest_cache','uploads','logs','loadtest'))
    for name in ('local_server.py','migrate.py','seed.py'): shutil.copyfile(old/'after'/name,dest/name)
p=root/'before/backend/app/services/credit/trading_context.py'
current=p.read_text();head=subprocess.check_output(['git','-C',str(source),'show','e3b2087f4828f76a79cfe152be768d3621829801:backend/app/services/credit/trading_context.py'],text=True)
start=current.index('async def read_dependencies(');end=current.index('\nasync def ',start+1)
hstart=head.index('async def read_dependencies(');hend=head.index('\nasync def ',hstart+1)
p.write_text(current[:start]+head[hstart:hend]+current[end:])
p.write_text(p.read_text().replace('from sqlalchemy import literal, select, union_all','from sqlalchemy import select'))
diff=subprocess.run(['diff','-ru','--exclude=__pycache__',str(root/'before/backend'),str(root/'after/backend')],capture_output=True,text=True).stdout
(root/'scope.diff').write_text(diff)
print(diff)
