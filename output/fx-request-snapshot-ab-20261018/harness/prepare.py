"""Preparation only. Does not start services or connect to PostgreSQL."""
from pathlib import Path
import argparse,subprocess,tarfile,io,hashlib,json,shutil
root=Path(__file__).resolve().parent;repo=Path('/tmp/sunyunbo/thccb-request-snapshot-20261018');output=Path('/data/sunyunbo/www/TouhouCCB/output/fx-request-snapshot-ab-20261018');baseline='330f4e683ec5564dea6ea59983351c3c8e128d9d'
parser=argparse.ArgumentParser();parser.add_argument('--after-commit');args=parser.parse_args()
for name,ref in [('before',baseline)]+([('after',args.after_commit)] if args.after_commit else []):
 dest=root/name
 if dest.exists():
  if name=='after':raise RuntimeError('after already exists; do not overwrite an experiment source')
  continue
 commit=subprocess.check_output(['git','-C',str(repo),'rev-parse',ref+'^{commit}'],text=True).strip();raw=subprocess.check_output(['git','-C',str(repo),'archive',commit,'backend']);dest.mkdir()
 with tarfile.open(fileobj=io.BytesIO(raw)) as archive:archive.extractall(dest,filter='data')
 for helper in ('migrate.py','seed.py'):shutil.copyfile(Path('/tmp/sunyunbo/fx-dependency-query-ab-20261018/after')/helper,dest/helper)
 hashes={str(p.relative_to(dest/'backend')):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((dest/'backend').rglob('*')) if p.is_file()}
 (output/(name+'-source.json')).write_text(json.dumps({'commit':commit,'hashes':hashes},indent=2)+'\n')
if (root/'after').exists():
 result=subprocess.run(['diff','-ru','--exclude=__pycache__',str(root/'before/backend'),str(root/'after/backend')],capture_output=True,text=True)
 (output/'source.diff').write_text(result.stdout)
print('prepared '+('before+after' if args.after_commit else 'before only; after awaits final commit'))
