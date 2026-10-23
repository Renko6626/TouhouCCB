#!/usr/bin/env python3
"""Isolated before/after SSE benchmark; never accepts a network application URL.

Reuses loadtest.fx_capacity's real scenario and sampler, always variant=current.
Run each cell with a NEW fxcapacity_*_test database. No DROP/RESET operation.
"""
from __future__ import annotations
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import random
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import httpx
import psycopg2
from psycopg2 import sql

HERE = Path(__file__).resolve().parent


def imports(repo):
    sys.path[:0] = [str(repo / 'loadtest'), str(repo / 'backend')]
    import fx_capacity
    return fx_capacity

async def seed(private, users):
    from sqlalchemy import select
    from app.core.database import async_session_maker, engine
    from app.models.base import SiteConfig, User
    from app.models.fx import FxPair
    from app.services.config_seed import seed_current_config
    from scripts.fx_loadtest import prepare, mint

    await seed_current_config()
    async with async_session_maker() as db, db.begin():
        if (await db.execute(select(User.id).limit(1))).first():
            raise ValueError("seed requires the newly created empty database")
        for key in ("fx_enabled", "fx_short_enabled"):
            row = (await db.execute(select(SiteConfig).where(SiteConfig.key == key))).scalar_one()
            row.value = "true"
        source = FxPair(currency_code="CAPSOURCE", currency_name="Isolated seed", status="trading",
                        gold_reserve=Decimal("10000000"), foreign_reserve=Decimal("10000000"),
                        short_lending_limit_foreign=Decimal("1000000"))
        db.add(source)
        await db.flush()
        data = await prepare(db, tag=private.name, users=users, cash=Decimal("10000"),
                             source_pair=source.id, manifest=private / "manifest.json")
        await mint(db, data, private / "tokens.json")
    await engine.dispose()


async def client(args):
    base = imports(Path(args.repo))
    Metrics, CapacityRunner, RoutedClient = base.Metrics, base.CapacityRunner, base.RoutedClient
    random.seed(54)
    credentials = json.loads((Path(args.private) / "tokens.json").read_text())
    if len(credentials["users"]) != args.users:
        raise ValueError("seed/client user count mismatch")
    output = Path(args.output)
    metrics = Metrics(output / "http")
    transport = httpx.AsyncHTTPTransport(uds=str(Path(args.private)/"app.sock"),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=100))

    async def sample_client():
        previous_cpu, previous_wall = time.process_time(), time.monotonic()
        with (output/"client-resources.jsonl").open("w") as log:
            while True:
                await asyncio.sleep(1)
                cpu, wall = time.process_time(), time.monotonic()
                log.write(json.dumps(dict(phase=metrics.phase, t=time.time(), interval_s=wall-previous_wall,
                    cpu_percent=100*(cpu-previous_cpu)/(wall-previous_wall)))+"\n")
                log.flush()
                previous_cpu, previous_wall = cpu, wall

    sampler = asyncio.create_task(sample_client())
    try:
        reader_socket = "app.sock"
        reader_transport = httpx.AsyncHTTPTransport(uds=str(Path(args.private)/reader_socket),
            limits=httpx.Limits(max_connections=150, max_keepalive_connections=100))
        stream_transport = httpx.AsyncHTTPTransport(uds=str(Path(args.private)/"app.sock"),
            limits=httpx.Limits(max_connections=400, max_keepalive_connections=400))
        # Three independent pools, identical for both applications and both user counts.
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost", timeout=15) as connection, \
                httpx.AsyncClient(transport=reader_transport, base_url="http://localhost", timeout=15) as reader, \
                httpx.AsyncClient(transport=stream_transport, base_url="http://localhost", timeout=15) as streams:
            runner = CapacityRunner(args, credentials, metrics,
                RoutedClient(connection, reader, streams))
            await runner.run_capacity()
    finally:
        sampler.cancel()
        await asyncio.gather(sampler, return_exceptions=True)
        summary = metrics.summary(dict(users=args.users, streams=2*args.users, requested_tps=args.rate,
            seconds=args.seconds, variant="current", label=args.label, transport="HTTP/1.1 over Unix socket",
            roles="60% cash / 20% financed / 20% short (including cross-zero)",
            started_at=datetime.now(timezone.utc).isoformat()))
        (output / "summary.json").write_text(json.dumps(summary, indent=2)+"\n")
        metrics.samples.close()


def reconcile(args, output, params):
    from fx_http import percentiles
    # Only aggregate balances/counts; no tokens or identities.
    connection = psycopg2.connect(dbname=args.database, **params)
    try:
        with connection.cursor() as cursor:
            cursor.execute('''SELECT (SELECT sum(cash-debt) FROM "user")::text,
                (SELECT sum(gold_reserve) FROM fx_pair WHERE currency_code LIKE 'LT%')::text,
                (SELECT sum(foreign_reserve) FROM fx_pair WHERE currency_code LIKE 'LT%')::text,
                (SELECT coalesce(sum(foreign_amount),0) FROM fx_wallet)::text,
                (SELECT coalesce(sum(principal_foreign),0) FROM fx_short_position)::text,
                (SELECT count(*) FROM fx_trade), (SELECT count(*) FROM fx_pool_event WHERE kind='trade'),
                (SELECT count(*) FROM fx_execution), (SELECT sum(economic_version) FROM "user")''')
            row = cursor.fetchone()
            cursor.execute('SELECT count(*) FROM "user"')
            actual_users = cursor.fetchone()[0]
            if actual_users != args.users:
                raise ValueError(f"unexpected seeded users: {actual_users}")
            integrity = dict(zip(("net_user_gold", "pool_gold", "pool_foreign", "wallet_foreign", "short_principal",
                                  "trade_legs", "trade_events", "executions", "sum_user_versions"), row))
            # Fees are zero in this isolated fixture. Nonzero default loan interest
            # is recorded separately; both physical quantities remain accountable.
            integrity["users"] = actual_users
            integrity["expected_user_cash"] = str(Decimal(args.users)*Decimal("10000"))
            integrity["gold_net_delta"] = str(Decimal(row[0])+Decimal(row[1])-(Decimal("10000000")+Decimal(args.users)*Decimal("10000")))
            integrity["foreign_physical_delta"] = str(Decimal(row[2])+Decimal(row[3])-Decimal(row[4])-Decimal("10000000"))
            cursor.execute("SELECT gold_balance, foreign_balance FROM fx_treasury")
            treasury_gold, treasury_foreign = cursor.fetchone()
            cursor.execute("""SELECT coalesce(sum(coalesce((payload->>'interest_accrued')::numeric,0)
                + coalesce((payload->>'interest')::numeric,0)),0) FROM audit_event
                WHERE event_type IN ('interest_accrual','loan_borrow','loan_repay')""")
            interest = cursor.fetchone()[0]
            integrity["accrued_gold_interest"] = str(interest)
            integrity["gold_with_treasury_and_interest_delta"] = str(Decimal(row[0])+Decimal(row[1])+treasury_gold+interest-(Decimal("20000000")+Decimal(args.users)*Decimal("10000")))
            integrity["foreign_with_treasury_delta"] = str(Decimal(row[2])+Decimal(row[3])+treasury_foreign-Decimal("20000000"))
            visibility = json.loads((output / "visibility-input.json").read_text())
            cursor.execute("SELECT trade_id, id FROM fx_pool_event WHERE trade_id IS NOT NULL")
            event_ids = dict(cursor.fetchall())
            latencies, missing = {}, Counter()
            for phase, started, ids in visibility["observations"]:
                found = [visibility["pool_event_seen"].get(str(event_ids.get(i))) for i in ids]
                if found and all(t is not None for t in found):
                    latencies.setdefault(phase, []).append((max(found)-started)*1000)
                else:
                    missing[phase] += 1
            (output / "visibility.json").write_text(json.dumps(dict(
                latency={k: percentiles(v) for k,v in latencies.items()}, missing=dict(missing)), indent=2)+"\n")
            cursor.execute("SHOW fsync")
            integrity["fsync"] = cursor.fetchone()[0]
            cursor.execute("SHOW synchronous_commit")
            integrity["synchronous_commit"] = cursor.fetchone()[0]
            integrity["checks_passed"] = (
                Decimal(integrity["gold_with_treasury_and_interest_delta"]) == 0
                and Decimal(integrity["foreign_with_treasury_delta"]) == 0
                and integrity["trade_legs"] == integrity["trade_events"])
            (output / "integrity.json").write_text(json.dumps(integrity, indent=2)+"\n")
            assert Decimal(integrity["gold_with_treasury_and_interest_delta"]) == 0, integrity
            assert Decimal(integrity["foreign_with_treasury_delta"]) == 0, integrity
            assert integrity["trade_legs"] == integrity["trade_events"], integrity
    finally:
        connection.close()


def stop(process):
    if process is not None and process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def run(args):
    repo = Path(args.repo).resolve()
    if not (repo/'loadtest/fx_capacity.py').is_file():
        raise ValueError('repo must contain the existing capacity harness')
    if not re.fullmatch(r'fxcapacity_[a-z0-9_]+_test', args.database or ''):
        raise ValueError('database must be a NEW fxcapacity_*_test name')
    if not Path(args.pg_socket).is_absolute() or not Path(args.pg_socket).is_dir():
        raise ValueError('requires an existing local PostgreSQL Unix socket directory')
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    private = Path(tempfile.mkdtemp(prefix='fxcap_'))
    params = dict(user=args.pg_user, host=args.pg_socket, port=args.pg_port)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(map(str, [HERE, repo/'backend', repo/'loadtest'])),
        APP_ENV='development', SECRET_KEY=secrets.token_urlsafe(48),
        DATABASE_URL=f'postgresql+asyncpg://{args.pg_user}@/{args.database}?host={args.pg_socket}&port={args.pg_port}',
        FX_CAPACITY_OUTPUT=str(output), FX_CAPACITY_VARIANT='current')
    # Never inherit a read-only role or old prototype from the caller.
    env.pop('FX_CAPACITY_ROLE', None)
    env.pop('THCCB_READ_ONLY_INSTANCE', None)
    server = None
    try:
        connection = psycopg2.connect(dbname='postgres', **params)
        try:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(args.database)))
        finally:
            connection.close()
        (output/'run.json').write_text(json.dumps(dict(database=args.database, label=args.label,
            users=args.users, streams=2*args.users, stream_pool=400, rate=args.rate, seconds=args.seconds,
            repo=str(repo), variant='current', admission_overrides=[],
            git_head=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip(),
            git_diff=subprocess.check_output(['git', 'diff', '--stat'], cwd=repo, text=True)), indent=2)+'\n')
        with (output/'migration.log').open('w') as log:
            subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'], cwd=repo/'backend',
                env=env, stdout=log, stderr=log, check=True)
        with (output/'seed.log').open('w') as log:
            subprocess.run([sys.executable, __file__, 'seed', '--repo', str(repo), '--private', str(private),
                '--users', str(args.users)], env=env, stdout=log, stderr=log, check=True)
        with (output/'server.log').open('w') as log:
            server = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'sampler:app', '--uds',
                str(private/'app.sock'), '--no-access-log', '--log-level', 'warning'],
                cwd=repo/'backend', env=env, stdout=log, stderr=log)
            try:
                for _ in range(600):
                    if server.poll() is not None:
                        raise RuntimeError('application startup failed; see server.log')
                    if (private/'app.sock').exists():
                        break
                    time.sleep(.1)
                else:
                    raise RuntimeError('application startup timed out')
                with (output/'client.log').open('w') as client_log:
                    subprocess.run([sys.executable, __file__, 'client', '--repo', str(repo), '--private', str(private),
                        '--output', str(output), '--label', args.label, '--users', str(args.users),
                        '--rate', str(args.rate), '--seconds', str(args.seconds)], env=env,
                        stdout=client_log, stderr=client_log, timeout=args.seconds+300, check=True)
            finally:
                stop(server)
        imports(repo)
        reconcile(args, output, params)
        print(json.dumps(dict(status='finished', output=str(output), database=args.database)))
    finally:
        stop(server)
        shutil.rmtree(private)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['run', 'seed', 'client'])
    parser.add_argument('--repo', required=True)
    parser.add_argument('--users', type=int, choices=[100, 200], default=100)
    parser.add_argument('--rate', type=float, choices=[1, 20, 54], default=54)
    parser.add_argument('--seconds', type=float, default=40)
    parser.add_argument('--label', choices=['baseline', 'candidate'], default='baseline')
    parser.add_argument('--database')
    parser.add_argument('--pg-socket', default='/tmp/sunyunbo/thccb-sse-20261023/pg-socket')
    parser.add_argument('--pg-port', type=int, default=55924)
    parser.add_argument('--pg-user', default='ssebench')
    parser.add_argument('--private')
    parser.add_argument('--output')
    parser.add_argument('--amount', default='10')
    args = parser.parse_args()
    if not 1 <= args.seconds <= 1800:
        parser.error('seconds must be in [1,1800]')
    if args.command == 'run':
        run(args)
    elif args.command == 'seed':
        imports(Path(args.repo).resolve())
        asyncio.run(seed(Path(args.private), args.users))
    else:
        asyncio.run(client(args))


if __name__ == '__main__':
    main()
