#!/usr/bin/env python3
"""Isolated PostgreSQL + real HTTP/SSE capacity experiment at a fixed arrival rate.

Run with the project's authorized Python environment. Requires a running local
PostgreSQL Unix socket and creates a NEW *_test database (never drops/reset one).
The application and load generator are separate processes using HTTP over UDS.
No production URL, credentials, firewall change or extra package is needed.
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
import signal
import subprocess
import sys
import tempfile
import time

import httpx
import psycopg2
from psycopg2 import sql

from fx_http import Metrics, Runner, percentiles

REPO = Path(__file__).resolve().parents[1]


class RoutedClient:
    """Equivalent to routing two existing GET endpoints to a read-only service.

    Quotes, writes, SSE and history remain on the economic owner, which owns
    the committed risk/pricing registry. No ownership check is bypassed.
    """
    def __init__(self, owner, reader, streams):
        self.owner, self.reader, self.streams = owner, reader, streams

    async def request(self, method, path, **kwargs):
        read = method == "GET" and (
            path == "/api/v1/user/trading-context" or path.endswith("/my-trades"))
        return await (self.reader if read else self.owner).request(method, path, **kwargs)

    def stream(self, *args, **kwargs):
        return self.streams.stream(*args, **kwargs)


async def seed(private):
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
        data = await prepare(db, tag=private.name, users=100, cash=Decimal("10000"),
                             source_pair=source.id, manifest=private / "manifest.json")
        await mint(db, data, private / "tokens.json")
    await engine.dispose()


class CapacityRunner(Runner):
    def phase(self, name):
        self.m.phase = name
        (self.m.directory / "phase.txt").write_text(name + "\n")

    async def fixed_stage(self, name, users, rate, seconds):
        """Never substitute a closed-loop achieved rate for requested arrivals."""
        self.phase(name)
        started = time.monotonic()
        stop = asyncio.Event()
        pollers = [asyncio.create_task(self.versions(u, started+seconds, stop)) for u in users]
        inflight = set()
        arrivals = Counter()
        sse_before = {k: dict(v) for k, v in self.m.sse_counts.items()}
        completed_before_deadline = 0
        cycle_latencies = []
        schedule_lags = []
        failures = []

        def completed(task):
            inflight.discard(task)
            if not task.cancelled() and task.exception() is not None:
                failures.append(str(task.exception()))

        async def cycle(user, due):
            nonlocal completed_before_deadline
            try:
                result = await self.trade(user)
                if result:
                    arrivals["successful_cycles"] += 1
                    completed_before_deadline += time.monotonic() <= started+seconds
                else:
                    arrivals["failed_cycles"] += 1
            finally:
                cycle_latencies.append((time.monotonic()-due)*1000)

        try:
            for index in range(round(rate*seconds)):
                due = started + index/rate
                await asyncio.sleep(max(0, due-time.monotonic()))
                if self.m.stop.is_set():
                    break
                arrivals["offered"] += 1
                schedule_lags.append((time.monotonic()-due)*1000)
                user = users[index % len(users)]
                if self.locks[user["uid"]].locked():
                    arrivals["busy_user_skipped"] += 1
                    continue
                task = asyncio.create_task(cycle(user, due))
                inflight.add(task)
                task.add_done_callback(completed)
            await asyncio.sleep(max(0, started+seconds-time.monotonic()))
            # Count drain separately; it must not inflate sustained throughput.
            at_deadline = dict(self.m.counts[(name, "trade")])
            await asyncio.gather(*inflight)
        finally:
            stop.set()
            for task in pollers:
                task.cancel()
            await asyncio.gather(*pollers, return_exceptions=True)
        elapsed = time.monotonic()-started
        self.m.stages.append(dict(name=name, requested_rate=rate, arrival_window_s=seconds,
            total_with_drain_s=elapsed, arrivals=dict(arrivals),
            trade_responses_by_deadline=at_deadline,
            completed_cycles_by_deadline=completed_before_deadline,
            completed_cycle_latency=percentiles(cycle_latencies),
            client_schedule_lag=percentiles(schedule_lags),
            sse_delta={k: dict(Counter(v)-Counter(sse_before.get(k, {}))) for k,v in self.m.sse_counts.items()},
            connected_at_end={k: len(v) for k,v in self.m.connected.items()}))
        if failures:
            raise RuntimeError(f"load generator cycle failed: {failures[:3]}")

    async def run_capacity(self):
        users = self.credentials["users"]
        snap = await self.request(None, "snapshot", "GET", f"/api/v1/fx/pairs/{self.pid}/snapshot")
        if not snap or snap["pair"]["currency_code"] != self.credentials["currency_code"] or not snap.get("history_ready"):
            raise ValueError("isolated FX market is not ready")
        streams = []
        try:
            self.phase("connect")
            for user in users:
                await self.connect_user(user, streams)
            await self.prepare_roles(users)
            await self.fixed_stage("warmup", users, 10, 10)
            await self.fixed_stage("capacity", users, self.args.rate, self.args.seconds)
            self.phase("visibility_drain")
            await asyncio.sleep(2)
            # Event IDs and trade IDs belong to different sequences. Reconcile
            # them using the persisted relation after stopping this isolated app.
            (self.m.directory.parent / "visibility-input.json").write_text(json.dumps(dict(
                observations=self.observations, pool_event_seen=self.m.pool_event_seen))+"\n")
            if self.m.stop.is_set():
                raise ValueError(self.m.reason)
        finally:
            for task in streams:
                task.cancel()
            await asyncio.gather(*streams, return_exceptions=True)


async def client(args):
    random.seed(54)
    credentials = json.loads((Path(args.private) / "tokens.json").read_text())
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
        reader_socket = "read.sock" if args.variant == "split_reads" else "app.sock"
        reader_transport = httpx.AsyncHTTPTransport(uds=str(Path(args.private)/reader_socket),
            limits=httpx.Limits(max_connections=150, max_keepalive_connections=100))
        stream_transport = httpx.AsyncHTTPTransport(uds=str(Path(args.private)/"app.sock"),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=200))
        # All variants have identical pool separation. Otherwise moving GETs
        # would also reduce httpx's scans over 200 occupied SSE connections,
        # confounding load-generator CPU savings with server process isolation.
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost", timeout=15) as connection, \
                httpx.AsyncClient(transport=reader_transport, base_url="http://localhost", timeout=15) as reader, \
                httpx.AsyncClient(transport=stream_transport, base_url="http://localhost", timeout=15) as streams:
            runner = CapacityRunner(args, credentials, metrics,
                RoutedClient(connection, reader, streams))
            await runner.run_capacity()
    finally:
        sampler.cancel()
        await asyncio.gather(sampler, return_exceptions=True)
        summary = metrics.summary(dict(users=100, streams=200, requested_tps=args.rate,
            seconds=args.seconds, variant=args.variant, transport="HTTP/1.1 over Unix socket",
            roles="60% cash / 20% financed / 20% short (including cross-zero)",
            started_at=datetime.now(timezone.utc).isoformat()))
        (output / "summary.json").write_text(json.dumps(summary, indent=2)+"\n")
        metrics.samples.close()


def run(args):
    if not re.fullmatch(r"fxcapacity_[a-z0-9_]+_test", args.database):
        raise ValueError("database must be a fresh fxcapacity_*_test name")
    if not Path(args.pg_socket).is_absolute() or not Path(args.pg_socket).is_dir():
        raise ValueError("only an existing local PostgreSQL Unix socket directory is accepted")
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    private = Path(tempfile.mkdtemp(prefix="fxcap_"))
    env = dict(os.environ, PYTHONPATH=f"{REPO / 'backend'}:{REPO / 'loadtest'}",
               APP_ENV="development", SECRET_KEY=secrets.token_urlsafe(48),
               DATABASE_URL=f"postgresql+asyncpg://{args.pg_user}@/{args.database}?host={args.pg_socket}&port={args.pg_port}",
               FX_CAPACITY_OUTPUT=str(output), FX_CAPACITY_VARIANT=args.variant)
    params = dict(user=args.pg_user, host=args.pg_socket, port=args.pg_port)
    connection = psycopg2.connect(dbname="postgres", **params)
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(args.database)))
    finally:
        connection.close()
    (output / "run.json").write_text(json.dumps(dict(database=args.database, variant=args.variant,
        rate=args.rate, seconds=args.seconds, private_directory=str(private),
        git_head=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()), indent=2)+"\n")
    with (output / "migration.log").open("w") as log:
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                       cwd=REPO/"backend", env=env, stdout=log, stderr=log, check=True)
    with (output / "seed.log").open("w") as log:
        subprocess.run([sys.executable, __file__, "seed", "--private", str(private)],
                       env=env, stdout=log, stderr=log, check=True)
    with (output / "server.log").open("w") as log:
        server = subprocess.Popen([sys.executable, "-m", "uvicorn", "fx_capacity_server:app", "--uds",
            str(private / "app.sock"), "--no-access-log", "--log-level", "warning"],
            cwd=REPO/"backend", env=env, stdout=log, stderr=log)
        reader_process = None
        reader_log = None
        try:
            for _ in range(600):
                if server.poll() is not None:
                    raise RuntimeError("application failed startup; inspect server.log")
                if (private/"app.sock").exists():
                    break
                time.sleep(.1)
            else:
                raise RuntimeError("application startup timed out")
            if args.variant == "split_reads":
                reader_output = output / "read-worker"
                reader_output.mkdir()
                reader_env = dict(env, THCCB_READ_ONLY_INSTANCE="true", FX_CAPACITY_ROLE="read",
                                  FX_CAPACITY_OUTPUT=str(reader_output))
                reader_log = (reader_output / "server.log").open("w")
                reader_process = subprocess.Popen([sys.executable, "-m", "uvicorn", "fx_capacity_server:app",
                    "--uds", str(private/"read.sock"), "--no-access-log", "--log-level", "warning"],
                    cwd=REPO/"backend", env=reader_env, stdout=reader_log, stderr=reader_log)
                for _ in range(600):
                    if reader_process.poll() is not None:
                        raise RuntimeError("read-only worker startup failed")
                    if (private/"read.sock").exists():
                        break
                    time.sleep(.1)
                else:
                    raise RuntimeError("read-only worker startup timed out")
            with (output / "client.log").open("w") as client_log:
                subprocess.run([sys.executable, __file__, "client", "--private", str(private),
                    "--output", str(output), "--variant", args.variant, "--rate", str(args.rate),
                    "--seconds", str(args.seconds)], env=env, stdout=client_log, stderr=client_log,
                    timeout=args.seconds+240, check=True)
        finally:
            if reader_process is not None:
                reader_process.send_signal(signal.SIGTERM)
                try:
                    reader_process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    reader_process.kill()
                    reader_process.wait()
            if reader_log is not None:
                reader_log.close()
            server.send_signal(signal.SIGTERM)
            try:
                server.wait(timeout=30)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
    # Persist only aggregate balances/counts; never token files or private identities.
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
            integrity = dict(zip(("net_user_gold", "pool_gold", "pool_foreign", "wallet_foreign", "short_principal",
                                  "trade_legs", "trade_events", "executions", "sum_user_versions"), row))
            # Fees are zero in this isolated fixture. Nonzero default loan interest
            # is recorded separately; both physical quantities remain accountable.
            integrity["gold_net_delta"] = str(Decimal(row[0])+Decimal(row[1])-Decimal("11000000"))
            integrity["foreign_physical_delta"] = str(Decimal(row[2])+Decimal(row[3])-Decimal(row[4])-Decimal("10000000"))
            cursor.execute("SELECT gold_balance, foreign_balance FROM fx_treasury")
            treasury_gold, treasury_foreign = cursor.fetchone()
            cursor.execute("""SELECT coalesce(sum(coalesce((payload->>'interest_accrued')::numeric,0)
                + coalesce((payload->>'interest')::numeric,0)),0) FROM audit_event
                WHERE event_type IN ('interest_accrual','loan_borrow','loan_repay')""")
            interest = cursor.fetchone()[0]
            integrity["accrued_gold_interest"] = str(interest)
            integrity["gold_with_treasury_and_interest_delta"] = str(Decimal(row[0])+Decimal(row[1])+treasury_gold+interest-Decimal("21000000"))
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
            (output / "integrity.json").write_text(json.dumps(integrity, indent=2)+"\n")
            assert integrity["gold_with_treasury_and_interest_delta"] == "0.000000", integrity
            assert integrity["foreign_with_treasury_delta"] == "0.000000", integrity
            assert integrity["trade_legs"] == integrity["trade_events"], integrity
    finally:
        connection.close()
    print(json.dumps(dict(output=str(output), database=args.database, status="finished")), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "seed", "client"))
    parser.add_argument("--database")
    parser.add_argument("--pg-socket", default="/tmp/touhouccb-pg-socket")
    parser.add_argument("--pg-port", type=int, default=55486)
    parser.add_argument("--pg-user", default=os.environ.get("USER"))
    parser.add_argument("--private")
    parser.add_argument("--output")
    parser.add_argument("--variant", choices=("current", "cadence8", "asgi_cadence8", "split_reads"), default="current")
    parser.add_argument("--rate", type=float, default=54)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--amount", default="10")
    args = parser.parse_args()
    if not 0 < args.rate <= 200 or not 1 <= args.seconds <= 1800:
        raise ValueError("rate must be in (0,200] and seconds in [1,1800]")
    if args.command == "run":
        run(args)
    elif args.command == "seed":
        asyncio.run(seed(Path(args.private)))
    else:
        asyncio.run(client(args))


if __name__ == "__main__":
    main()
