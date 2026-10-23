#!/usr/bin/env python3
"""Summarize all 12 A/B cells, retaining missing cells and raw measurement detail."""
import argparse
import gzip
import importlib.util
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO/'loadtest'))
spec = importlib.util.spec_from_file_location('previous_summary', REPO/'output/fx-capacity-54-20261018/summarize.py')
previous = importlib.util.module_from_spec(spec)
spec.loader.exec_module(previous)


def read_json(path):
    return json.loads(path.read_text())


def json_lines(path):
    content = path.read_text() if path.exists() else gzip.open(str(path)+'.gz', 'rt').read()
    return [json.loads(line) for line in content.splitlines()]


def summarize(path):
    summary, run = read_json(path/'summary.json'), read_json(path/'run.json')
    stage = next(s for s in summary['stages'] if s['name'] == 'capacity')
    trade = next((r for r in summary['requests'] if r['phase'] == 'capacity' and r['name'] == 'trade'),
                 {'results': {}, 'latency_by_result': {}})
    owner_rows = [r for r in json_lines(path/'resources.jsonl') if r['phase'] == 'capacity']
    owner = previous.resources(path/'resources.jsonl') if owner_rows else {'samples': 0}
    owner['rss_mb_peak'] = max((r['rss_mb'] for r in owner_rows), default=None)
    client_rows = [r for r in json_lines(path/'client-resources.jsonl') if r['phase'] == 'capacity']
    elapsed = sum(r['interval_s'] for r in client_rows)
    integrity = read_json(path/'integrity.json')
    return dict(users=run['users'], rate=run['rate'], label=run['label'], run=path.name, status='completed' if integrity.get('checks_passed') else 'integrity_failed',
        abort_reason=summary['abort_reason'], seconds=stage['arrival_window_s'],
        success_tps=stage['trade_responses_by_deadline'].get('ok', 0)/stage['arrival_window_s'],
        completed_cycles_tps=stage['completed_cycles_by_deadline']/stage['arrival_window_s'],
        post_latency=trade['latency_by_result'], post_results=trade['results'],
        cycle_latency=stage['completed_cycle_latency'], arrivals=stage['arrivals'],
        skipped=stage['arrivals'].get('busy_user_skipped', 0),
        rejected=sum(n for k,n in trade['results'].items() if k != 'ok'),
        capacity_request_results={r['name']: r['results'] for r in summary['requests'] if r['phase'] == 'capacity'},
        owner=owner, connected=stage['connected_at_end'], sse=stage['sse_delta'],
        client_cpu_percent=sum(r['cpu_percent']*r['interval_s'] for r in client_rows)/elapsed if elapsed else None,
        client_schedule_lag=stage['client_schedule_lag'], visibility=read_json(path/'visibility.json'),
        integrity=integrity, coverage=summary['coverage'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=HERE)
    args = parser.parse_args()
    found = {}
    for metadata in sorted(args.root.glob('*/run.json')):
        run = read_json(metadata)
        key = (run['users'], run['rate'], run['label'])
        if key in found:
            raise ValueError(f'duplicate cell {key}; choose a root with one run per cell')
        path = metadata.parent
        if all((path/name).exists() for name in ['summary.json', 'integrity.json', 'visibility.json']):
            found[key] = summarize(path)
        else:
            found[key] = dict(users=key[0], rate=key[1], label=key[2], run=path.name, status='incomplete')
    rows = [found.get((users, rate, label), dict(users=users, rate=rate, label=label, status='missing'))
            for users in [100,200] for rate in [1,20,54] for label in ['baseline','candidate']]
    result = dict(boundaries=['Visibility is only the first public observer; private per-frame delivery is not proven.',
        'Existing optimized SQLAlchemy path is retained.', 'No ASGI middleware or read-process experiment.'], rows=rows)
    (args.root/'comparison.json').write_text(json.dumps(result, indent=2)+'\n')
    print('| Users | Rate | App | Status | Success TPS | POST P95/P99 ms | Cycle P95/P99 ms | Owner CPU/RSS MiB/SQL s | Queue peak | Client CPU | Skips/rejects | Visibility missing |')
    print('|---:|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|')
    for r in rows:
        prefix=f"| {r['users']} | {r['rate']} | {r['label']} | {r['status']} |"
        if r['status'] != 'completed':
            print(prefix+' — | — | — | — | — | — | — | — |')
            continue
        post=r['post_latency'].get('ok', {})
        cycle=r['cycle_latency']
        owner=r['owner']
        print(prefix+f" {r['success_tps']:.2f} | {post.get('p95_ms')}/{post.get('p99_ms')} | "
              f"{cycle['p95_ms']}/{cycle['p99_ms']} | {owner.get('cpu_percent')}/{owner.get('rss_mb_peak')}/{owner.get('sql_per_s')} | "
              f"{owner.get('queue_peak')} | {r['client_cpu_percent']} | {r['skipped']}/{r['rejected']} | "
              f"{r['visibility']['missing'].get('capacity', 0)} |")


if __name__ == '__main__':
    main()
