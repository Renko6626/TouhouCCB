"""Fixed-compose adapter for the existing identity/entitlement rebuild tool."""
from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy.engine import make_url
from app.core.config import settings
from scripts import rebuild_user_database as rebuild


def fixed_source_url():
    url = make_url(settings.build_db_url())
    if url.query or url.get_backend_name() != 'postgresql' or (url.host, url.port or 5432, url.username, url.database) != ('postgres', 5432, 'thccb', 'thccb'):
        raise ValueError('production rebuild requires fixed compose PostgreSQL source')
    return url


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('stage', choices=['config', 'preview', 'export', 'import', 'verify'])
    p.add_argument('--run-id', required=True)
    p.add_argument('--balance', default='0')
    p.add_argument('--directory', required=True)
    p.add_argument('--after-cutover', action='store_true')
    args = p.parse_args()
    if not re.fullmatch(r'[0-9]{1,20}', args.run_id):
        raise ValueError('invalid run ID')
    url = fixed_source_url()
    policy = {'balance': args.balance, 'dependency_balance': '0'}
    rebuild.balance(policy)
    if args.stage == 'config':
        print('fixed PostgreSQL source validated')
        return
    folder = Path(args.directory)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    manifest = folder / 'manifest.json'
    policy_path = folder / 'policy.json'
    source = url.set(database='thccb_before_' + args.run_id) if args.after_cutover else url
    target = url if args.after_cutover else url.set(database='thccb_rebuild_' + args.run_id)
    if args.stage in ('preview', 'export'):
        if args.after_cutover:
            raise ValueError('cannot export after cutover')
        data = rebuild.export_data(source, policy)
        # Validate before any target creation or cutover; includes unknown refs.
        rebuild.validate(data)
        rebuild.write_private(policy_path, policy)
        rebuild.write_private(manifest, data)
        print(json.dumps({'users': len(data['real_ids']), 'dependency_users': len(data['dependencies']), 'balance': str(rebuild.balance(policy)), 'source_summary': data['source_summary'], 'counts': {n: len(v) for n, v in data['rows'].items()}}))
    else:
        data = json.loads(manifest.read_text())
        if data['policy'] != policy or json.loads(policy_path.read_text()) != policy:
            raise ValueError('policy differs from exported manifest')
        if args.stage == 'import':
            if args.after_cutover:
                raise ValueError('cannot import after cutover')
            rebuild.import_data(source, target, data)
        print(json.dumps(rebuild.verify(target, data)))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('production rebuild failed: ' + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__), file=sys.stderr)
        sys.exit(1)
