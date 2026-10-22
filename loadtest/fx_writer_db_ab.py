"""Alternate three source variants on fresh local PostgreSQL databases.

Run with the project's Python dependencies installed. Each variant directory
contains app/. The server must be dedicated to this experiment and durable.
All created databases are retained; this script never drops existing data.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

import psycopg2
from psycopg2 import sql


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--query-only', type=Path, required=True)
    parser.add_argument('--combined', type=Path, required=True)
    parser.add_argument('--socket', type=Path, required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--user', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--operations', type=int, default=32)
    args = parser.parse_args()
    if not str(args.socket.resolve()).startswith('/tmp/') or args.repeats < 1:
        parser.error('requires an isolated /tmp Unix socket and repeats >= 1')
    args.output.mkdir(parents=True, exist_ok=True)
    variants = {'baseline': args.baseline, 'queries': args.query_only, 'combined': args.combined}
    harness = Path(__file__).with_name('fx_writer_db.py')
    results = []
    sequence = 0
    conn = psycopg2.connect(dbname='postgres', user=args.user, host=str(args.socket), port=args.port)
    conn.autocommit = True
    try:
        # Diagnostics are separate from unprofiled capacity measurements.
        for repeat in range(args.repeats + 1):
            diagnostic = repeat == 0
            names = list(variants)
            offset = repeat % len(names)
            names = names[offset:] + names[:offset]
            for scenario, pairs in [('cash', 1), ('finance', 1), ('legacy_finance', 1), ('short', 1), ('finance', 4)]:
                for variant in names:
                    sequence += 1
                    name = f'fx_ab_{os.getpid()}_{sequence}_test'
                    with conn.cursor() as cursor:
                        cursor.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
                    label = f'{repeat}-{scenario}-p{pairs}-{variant}'
                    destination = args.output / f'{label}.json'
                    # Database/socket/user here are local experiment identifiers.
                    from sqlalchemy.engine import URL
                    url = URL.create('postgresql+asyncpg', username=args.user, database=name,
                                     query={'host': str(args.socket), 'port': str(args.port)})
                    command = [sys.executable, str(harness), '--backend', str(variants[variant]),
                               '--database-url', url.render_as_string(hide_password=False),
                               '--output', str(destination), '--scenario', scenario,
                               '--clients', '16', '--pairs', str(pairs),
                               '--operations', '4' if diagnostic else str(args.operations)]
                    if diagnostic:
                        command.append('--sql')
                    print(f'Starting {label}', flush=True)
                    with (args.output / f'{label}.log').open('w') as log:
                        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
                    result = json.loads(destination.read_text())
                    results.append({'variant': variant, 'repeat': repeat, 'file': destination.name,
                                    'database': name, **{key: result[key] for key in (
                                        'scenario', 'pairs', 'commands', 'throughput_per_s', 'handler_ms',
                                        'latency_ms', 'queue_wait_mean_ms', 'process_cpu_ms_per_command',
                                        'sql_per_command', 'select_per_command', 'errors')}})
                    (args.output / 'summary.json').write_text(json.dumps(results, indent=2) + '\n')
                    print(f"Finished {label}: {result['throughput_per_s']:.2f}/s", flush=True)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
