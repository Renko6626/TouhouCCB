"""Archive committed sources only. No service start or database connection."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parent
REPO = Path('/tmp/sunyunbo/thccb-single-writer-20261018')
OUTPUT = Path('/data/sunyunbo/www/TouhouCCB/output/fx-single-writer-ab-20261018')
PRIOR = Path('/tmp/sunyunbo/fx-request-snapshot-ab-20261018')
BASELINE = '330f4e683ec5564dea6ea59983351c3c8e128d9d'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--after-commit', help='Only use after the controller supplies the final committed ref')
args = parser.parse_args()
for name, ref in [('before', BASELINE)] + ([('after', args.after_commit)] if args.after_commit else []):
    dest = ROOT / name
    if dest.exists():
        if name == 'after':
            raise RuntimeError('after already exists; never overwrite experiment sources')
        continue
    commit = subprocess.check_output(['git', '-C', str(REPO), 'rev-parse', ref + '^{commit}'], text=True).strip()
    raw = subprocess.check_output(['git', '-C', str(REPO), 'archive', commit, 'backend'])
    dest.mkdir()
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        archive.extractall(dest, filter='data')
    for helper in ('migrate.py', 'seed.py'):
        shutil.copyfile(PRIOR / 'before' / helper, dest / helper)
    files = {str(p.relative_to(dest)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(dest.rglob('*')) if p.is_file()}
    (OUTPUT / (name + '-source.json')).write_text(json.dumps({
        'commit': commit, 'archive': 'git archive COMMIT backend', 'hashes': files,
        'seed_helper_origin': str(PRIOR / 'before'),
    }, indent=2) + '\n')
if (ROOT / 'after').exists():
    result = subprocess.run(['diff', '-ru', '--exclude=__pycache__', str(ROOT / 'before/backend'), str(ROOT / 'after/backend')], capture_output=True, text=True)
    (OUTPUT / 'source.diff').write_text(result.stdout)
print('prepared before+after' if args.after_commit else 'prepared before only; after awaits controller final commit')
