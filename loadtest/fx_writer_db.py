"""Isolated PostgreSQL benchmark of the real FX queue and committed money path.

No HTTP/SSE/schedulers: measures writer capacity, not whole-site capacity.
Use an empty, disposable *_test database; never drops or truncates data.
Run each source variant in a fresh process/database, alternating variant order.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextvars import ContextVar
from datetime import datetime, timezone
from decimal import Decimal as D
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import time


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', type=Path, required=True)
    parser.add_argument('--database-url', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--scenario', choices=['cash', 'finance', 'legacy_finance', 'short'], required=True)
    parser.add_argument('--clients', type=int, default=16)
    parser.add_argument('--operations', type=int, default=32, help='Measured commands per client (even).')
    parser.add_argument('--pairs', type=int, default=1)
    parser.add_argument('--sql', action='store_true', help='Diagnostic cursor timing/counting; omit for capacity runs.')
    args = parser.parse_args()
    if not 1 <= args.pairs <= args.clients <= 128 or args.operations < 2 or args.operations % 2:
        parser.error('require 1 <= pairs <= clients <= 128 and even operations >= 2')
    return args


def distribution(values):
    ordered = sorted(values)
    return {'mean': statistics.mean(ordered),
            'p50': ordered[int((len(ordered) - 1) * .50)],
            'p95': ordered[int((len(ordered) - 1) * .95)],
            'p99': ordered[int((len(ordered) - 1) * .99)]}


async def run(args):
    # Validate destination before importing app (which constructs its engine).
    from sqlalchemy import event, func, inspect, select, text
    from sqlalchemy.engine import make_url
    url = make_url(args.database_url)
    host = url.host or url.query.get('host', '')
    if (url.drivername != 'postgresql+asyncpg' or not (url.database or '').endswith('_test')
            or not (host in {'localhost', '127.0.0.1', '::1'} or host.startswith('/tmp/'))):
        raise ValueError('requires a local PostgreSQL *_test database')
    os.environ['DATABASE_URL'] = args.database_url
    sys.path.insert(0, str(args.backend.resolve()))
    import app.main  # Register exactly the application's models; no lifespan.
    from sqlmodel import SQLModel
    from app.core import database
    from app.models.base import SiteConfig, User
    from app.models.audit import AuditEvent
    from app.models.ledger import LedgerEntry
    from app.models.fx import FxExecution, FxPair, FxPoolEvent, FxShortPosition, FxTrade, FxTreasury, FxWallet
    from app.services import site_config
    from app.services.credit import flags
    from app.services.credit.ownership import OWNERSHIP
    from app.services.trading_prices import REGISTRY
    from app.services.fx.writer import FX_WRITER
    from app.services.fx.writer_ops import BorrowBuy, NetTrade, ShortOpen, ShortCover

    engine, factory = database.engine, database.async_session_maker
    async with engine.begin() as conn:
        tables = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
        if tables:
            raise ValueError('refusing a nonempty database')
        await conn.run_sync(SQLModel.metadata.create_all)
        settings = {name: (await conn.execute(text('SHOW ' + name))).scalar_one()
                    for name in ('server_version', 'fsync', 'synchronous_commit', 'full_page_writes')}
    assert settings['fsync'] == settings['synchronous_commit'] == settings['full_page_writes'] == 'on'
    await OWNERSHIP.acquire(required=True)
    REGISTRY.configure(memory_products=frozenset({'fx', 'lmsr'}))
    flags.set_flags(flags.CreditFlags(credit_leverage=D('4'), credit_maintenance_ratio=D('.1')))
    site_config.clear_cache()
    now = datetime.now(timezone.utc)
    async with factory() as db:
        pairs = [FxPair(currency_code=f'BENCH{i}', currency_name='Benchmark', status='trading',
                        gold_reserve=D('100000000'), foreign_reserve=D('100000000'),
                        buy_fee_rate=D('.001'), sell_fee_rate=D('.001'),
                        short_lending_limit_foreign=D('10000000')) for i in range(args.pairs)]
        users = [User(username=f'bench{i}', cash=D('1000000'), tos_accepted_at=now)
                 for i in range(args.clients)]
        db.add_all(pairs + users)
        await db.flush()
        for pair in pairs:
            db.add(FxTreasury(pair_id=pair.id, gold_balance=D('1000000'), foreign_balance=D('1000000')))
        for i, user in enumerate(users):
            db.add(FxWallet(user_id=user.id, pair_id=pairs[i % args.pairs].id))
        for key, value, kind in [('fx_enabled', 'true', 'bool'), ('loan_enabled', 'true', 'bool'),
                                 ('fx_short_enabled', 'true', 'bool'), ('loan_daily_rate', '0', 'decimal'),
                                 ('sell_fee_rate', '0', 'decimal'), ('credit_new_risk_frozen', 'false', 'bool')]:
            db.add(SiteConfig(key=key, value=value, value_type=kind))
        await db.commit()
        identities = [(user.id, pairs[i % args.pairs].id) for i, user in enumerate(users)]

    measured = False
    current = ContextVar('writer_measurement', default=None)
    records, latencies, errors = [], [], []
    sql_shapes = Counter()
    max_queue = 0

    if args.sql:
        from sqlalchemy.ext.asyncio import AsyncSession
        for method_name in ('flush', 'commit'):
            original = getattr(AsyncSession, method_name)
            async def timed_method(session, *positional, original=original, method_name=method_name, **keywords):
                record = current.get()
                started = time.perf_counter()
                try:
                    return await original(session, *positional, **keywords)
                finally:
                    if record is not None:
                        record[method_name + '_ms'] += (time.perf_counter() - started) * 1000
            setattr(AsyncSession, method_name, timed_method)

        @event.listens_for(engine.sync_engine, 'before_cursor_execute')
        def before_cursor(conn, cursor, statement, parameters, context, executemany):
            record = current.get()
            if record is not None:
                record['cursor_started'] = time.perf_counter()
                record['sql_count'] += 1
                record['select_count'] += int(statement.lstrip().upper().startswith('SELECT'))
                sql_shapes[statement] += 1

        @event.listens_for(engine.sync_engine, 'after_cursor_execute')
        def after_cursor(conn, cursor, statement, parameters, context, executemany):
            record = current.get()
            if record is not None:
                record['sql_ms'] += (time.perf_counter() - record['cursor_started']) * 1000

    await FX_WRITER.start()
    for command_type, operation in list(FX_WRITER._ops.items()):
        async def measured_operation(writer, command, operation=operation):
            if not measured:
                return await operation(writer, command)
            record = {'sql_count': 0, 'select_count': 0, 'sql_ms': 0.0,
                      'flush_ms': 0.0, 'commit_ms': 0.0, 'command': type(command).__name__}
            token = current.set(record)
            started = time.perf_counter()
            try:
                return await operation(writer, command)
            finally:
                record['handler_ms'] = (time.perf_counter() - started) * 1000
                current.reset(token)
                record.pop('cursor_started', None)
                records.append(record)
        FX_WRITER.register_op(command_type, measured_operation)

    async def totals():
        async with factory() as db:
            async def total(column):
                return (await db.execute(select(func.coalesce(func.sum(column), 0)))).scalar_one()
            gold = sum([await total(c) for c in (User.cash, FxPair.gold_reserve, FxTreasury.gold_balance)])
            gold -= await total(User.debt)
            foreign = sum([await total(c) for c in (FxPair.foreign_reserve, FxTreasury.foreign_balance, FxWallet.foreign_amount)])
            counts = {model.__tablename__: (await db.execute(select(func.count()).select_from(model))).scalar_one()
                      for model in (FxTrade, FxExecution, FxPoolEvent, AuditEvent, LedgerEntry)}
            versions = await total(FxPair.pool_version)
            return {'gold_net': str(gold), 'foreign': str(foreign), 'pool_versions': int(versions), 'counts': counts}

    versions = [0] * args.clients

    async def client(index, operations, phase):
        nonlocal max_queue
        uid, pid = identities[index]
        for step in range(operations):
            key = f'{phase}:{index}:{step}'
            if args.scenario in {'cash', 'finance'}:
                borrow = D('5') if args.scenario == 'finance' else D('0')
                cmd = NetTrade(pid, uid, 'buy', D('10'), borrow, True, D('0'), key, versions[index])
                increment = 2 if borrow else 1
            elif args.scenario == 'legacy_finance':
                cmd = BorrowBuy(pid, uid, D('10'), D('5'), D('0'), key)
                increment = 2
            elif step % 2 == 0:
                cmd = ShortOpen(pid, uid, D('10'), D('0'), key)
                increment = 1
            else:
                cmd = ShortCover(pid, uid, None, True, D('100'), key)
                increment = 1
            if measured:
                max_queue = max(max_queue, FX_WRITER._queues[pid].qsize())
            started = time.perf_counter()
            try:
                await FX_WRITER.submit(cmd)
                versions[index] += increment
            except Exception as exc:
                errors.append({'client': index, 'step': step, 'error': repr(exc)})
                raise
            finally:
                if measured:
                    latencies.append((time.perf_counter() - started) * 1000)

    try:
        initial = await totals()
        # Warm all command types, statements, connections and account rows.
        await asyncio.gather(*(client(i, 2, 'warm') for i in range(args.clients)))
        before = await totals()
        cpu_started, started = time.process_time(), time.perf_counter()
        measured = True
        await asyncio.gather(*(client(i, args.operations, 'measure') for i in range(args.clients)))
        measured = False
        elapsed, cpu = time.perf_counter() - started, time.process_time() - cpu_started
        after = await totals()
        count = args.clients * args.operations
        assert after['gold_net'] == initial['gold_net'], (initial, after)
        assert after['foreign'] == initial['foreign'], (initial, after)
        assert after['counts']['fx_trade'] - before['counts']['fx_trade'] == count
        assert after['counts']['fx_pool_event'] - before['counts']['fx_pool_event'] == count
        assert after['pool_versions'] - before['pool_versions'] == count
        async with factory() as db:
            missing = (await db.execute(select(func.count()).select_from(FxTrade).outerjoin(
                AuditEvent, (AuditEvent.ref_id == FxTrade.id) & (AuditEvent.event_type == 'fx_trade')
            ).where(AuditEvent.id.is_(None)))).scalar_one()
            assert missing == 0, 'trade without audit'
            if args.scenario == 'short':
                assert (await db.execute(select(func.sum(FxShortPosition.principal_foreign + FxShortPosition.interest_foreign)))).scalar_one() == 0
        source_hashes = {str(path.relative_to(args.backend)): hashlib.sha256(path.read_bytes()).hexdigest()
                         for path in sorted((args.backend / 'app').rglob('*.py'))}
        result = {'scenario': args.scenario, 'clients': args.clients, 'pairs': args.pairs,
                  'commands': count, 'sql_instrumented': args.sql, 'elapsed_s': elapsed,
                  'throughput_per_s': count / elapsed, 'process_cpu_ms_per_command': cpu * 1000 / count,
                  'latency_ms': distribution(latencies), 'handler_ms': distribution([r['handler_ms'] for r in records]),
                  'queue_wait_mean_ms': statistics.mean(latencies) - statistics.mean(r['handler_ms'] for r in records),
                  'queue_max_observed': max_queue, 'errors': errors, 'postgres': settings,
                  'sql_per_command': sum(r['sql_count'] for r in records) / count if args.sql else None,
                  'select_per_command': sum(r['select_count'] for r in records) / count if args.sql else None,
                  'sql_ms_per_command': sum(r['sql_ms'] for r in records) / count if args.sql else None,
                  'explicit_flush_ms_per_command': sum(r['flush_ms'] for r in records) / count if args.sql else None,
                  'commit_ms_per_command': sum(r['commit_ms'] for r in records) / count if args.sql else None,
                  'sql_shapes': dict(sql_shapes), 'initial': initial, 'before': before, 'after': after,
                  'source_hashes': source_hashes}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps({k: v for k, v in result.items() if k not in {'sql_shapes', 'source_hashes', 'initial', 'before', 'after'}}))
    finally:
        await FX_WRITER.stop()
        await OWNERSHIP.release()
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(run(arguments()))
