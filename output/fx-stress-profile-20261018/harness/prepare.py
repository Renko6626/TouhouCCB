import shutil,hashlib,json,subprocess
from pathlib import Path
r=Path(__file__).resolve().parent;old=Path('/tmp/sunyunbo/fx-dependency-query-ab-20261018');out=Path('/data/sunyunbo/www/TouhouCCB/output/fx-stress-profile-20261018');src=Path('/tmp/sunyunbo/thccb-fx-read-checkpoint-20261017/backend')
for v in ('control','profile'):
 shutil.copytree(src,r/v/'backend',ignore=shutil.ignore_patterns('venv','.env','*.db','__pycache__','.pytest_cache','uploads','logs','loadtest'))
 for f in ('migrate.py','seed.py'):shutil.copyfile(old/'after'/f,r/v/f)
for f in ('fx_http.py','compare_driver.py'):shutil.copyfile(old/f,r/f)
hashes={v:{str(f.relative_to(r/v/'backend')):hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted((r/v/'backend').rglob('*')) if f.is_file()} for v in ('control','profile')}
assert hashes['control']==hashes['profile']
(out/'source-hashes.json').write_text(json.dumps(hashes,indent=2)+'\n');(out/'baseline.json').write_text(json.dumps({'HEAD':subprocess.check_output(['git','-C',str(src.parent),'rev-parse','HEAD'],text=True).strip(),'status':subprocess.check_output(['git','-C',str(src.parent),'status','--short'],text=True)},indent=2)+'\n')
