"""Offline fixed-cutoff source conversion and event-based FX candle rebuild.

Stop economic producers before running. Ownership + pair GATE/row lock protect
one atomic transaction per pair. Dry-run validates the same source chain without
writes. Sources and financial state are never rewritten. Example:
    python -m scripts.backfill_fx_candles --all --dry-run
    python -m scripts.backfill_fx_candles --pair-id 3 --yes
    python -m scripts.backfill_fx_candles --pair-id 3 --through-event-id 123 --yes
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Optional, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.config import settings  # noqa: E402
from app.core.database import async_session_maker, engine  # noqa: E402
from app.models.fx import FxCandle, FxMarketDataState, FxPair, FxTrade  # noqa: E402
from app.services.credit.gates import GATES  # noqa: E402
from app.services.credit.keys import GroupKey  # noqa: E402
from app.services.credit.ownership import OWNERSHIP  # noqa: E402
from app.services.fx.candles import (  # noqa: E402
    apply_candle_batch,
    compute_fx_candle_rows,
    new_history_version,
)

PAGE_SIZE = 1000


def _require_offline_runtime(pair_id: int) -> None:
    """Reject an in-process rebuild while this process owns the live runtime.

    ``rebuild_pair`` is an offline maintenance operation.  The production CLI
    additionally requires the economic write ownership, so it refuses while a
    live backend holds it.  This guard covers the remaining case: a caller that
    already started the in-process owner runtime, whose flusher concurrently
    locks the same state row and writes candles (state/FK lock deadlock risk).
    Readers (``write_owner=False``) are not rejected; their stale ring is
    already generation-checked by the read paths.
    """
    from app.services.fx.market_state import FX_MARKET_DATA
    if FX_MARKET_DATA.write_owner and FX_MARKET_DATA.started:
        raise RuntimeError(
            "rebuild_pair is offline maintenance: the in-process FX market-data "
            f"owner runtime is started (pair {pair_id}); stop the backend first"
        )


async def rebuild_pair(db: AsyncSession, pair_id: int,
                       through_trade_id: Optional[int] = None, *,
                       through_event_id: Optional[int] = None, dry_run: bool = False) -> int:
    """Validate fixed source cutoffs, import missing events, rebuild atomically.

    Caller owns the offline GATE and transaction. A failure rolls back all work;
    repeated completed runs reuse source identities. Historical reconstruction
    assigns IDs above the global high-water and invalidates the generation.
    """
    from types import SimpleNamespace
    from app.models.audit import AuditEvent
    from app.models.fx import FxPoolEvent
    from app.services.fx.candles import load_market_data_state
    from scripts.fx_history_sources import merged_sources, order_group

    _require_offline_runtime(pair_id)
    if through_trade_id is not None:
        raise ValueError('--through-trade-id is no longer supported; use --through-event-id for derived candle cutoff')
    if through_event_id is not None and through_event_id < 0:
        raise ValueError('--through-event-id must be nonnegative')
    pair = (await db.execute(select(FxPair).where(FxPair.id == pair_id).with_for_update())).scalars().first()
    if pair is None:
        raise ValueError(f'FX pair {pair_id} not found')
    trade_cutoff = int((await db.execute(select(func.max(FxTrade.id)).where(FxTrade.pair_id == pair_id))).scalar() or 0)
    audit_cutoff = int((await db.execute(select(func.max(AuditEvent.id)).where(AuditEvent.ref_table == 'fx_pair', AuditEvent.ref_id == pair_id, AuditEvent.event_type.in_(['fx_fund', 'fx_withdraw'])))).scalar() or 0)
    existing_count = int((await db.execute(select(func.count()).select_from(FxPoolEvent).where(FxPoolEvent.pair_id == pair_id))).scalar_one())
    high_water = int((await db.execute(select(func.max(FxPoolEvent.id)))).scalar() or 0)
    if db.bind.dialect.name == 'postgresql':
        from sqlalchemy import text
        sequence_value = (await db.execute(text("SELECT last_value FROM pg_sequences WHERE schemaname = current_schema() AND sequencename = split_part(pg_get_serial_sequence('fx_pool_event','id'), '.', 2)"))).scalar()
        high_water = max(high_water, int(sequence_value or 0))
    current = None
    group = []
    previous_time = None
    previous_id = 0
    matched = 0
    missing_seen = False
    reconstruct = False
    total = 0
    validated = []
    def save_group():
        nonlocal current, previous_id, matched, missing_seen, reconstruct, total
        ordered, current = order_group(group, current)
        for data, old_id in ordered:
            if old_id is None:
                missing_seen = True
            else:
                matched += 1
                if missing_seen or old_id <= previous_id:
                    reconstruct = True
                previous_id = old_id
            validated.append((data, old_id))
            total += 1
    async for item in merged_sources(db, pair_id, trade_cutoff, audit_cutoff):
        at = item[0]['created_at']
        if group and at != previous_time:
            save_group()
            group = []
        group.append(item)
        previous_time = at
    if group:
        save_group()
    if not total or current != (pair.gold_reserve, pair.foreign_reserve):
        raise ValueError('history has no initialize or final reserves differ from locked pair')
    if matched != existing_count:
        raise ValueError('existing pool events lack reconstructible source linkage')
    if through_event_id is not None and reconstruct:
        raise ValueError('event cutoff cannot survive required ID reconstruction; run full conversion first')
    if dry_run:
        print(f'  pair={pair_id}: sources={total}, existing={matched}, trade_cutoff={trade_cutoff}, audit_cutoff={audit_cutoff}, reconstruct={reconstruct}')
        return high_water
    await db.execute(delete(FxCandle).where(FxCandle.pair_id == pair_id))
    state = await load_market_data_state(db, pair_id)
    version = new_history_version()
    state.history_version = version
    state.last_event_id = 0
    state.last_trade_id = 0
    state.history_ready = False
    await db.flush()
    if reconstruct:
        await db.execute(delete(FxPoolEvent).where(FxPoolEvent.pair_id == pair_id))
    after = 0
    batch = []
    next_id = high_water
    for data, old_id in validated:
        if reconstruct or old_id is None:
            next_id += 1
            old_id = next_id
            db.add(FxPoolEvent(id=old_id, **data))
        data['id'] = old_id
        if through_event_id is None or old_id <= through_event_id:
            batch.append(SimpleNamespace(**data))
        if len(batch) == PAGE_SIZE:
            await db.flush()
            await apply_candle_batch(db, pair_id=pair_id, expected_event_id=after,
                through_event_id=batch[-1].id, rows=compute_fx_candle_rows(batch), history_version=version)
            after = batch[-1].id
            batch = []
    await db.flush()
    if batch:
        await apply_candle_batch(db, pair_id=pair_id, expected_event_id=after,
            through_event_id=batch[-1].id, rows=compute_fx_candle_rows(batch), history_version=version)
        after = batch[-1].id
    # Explicit partial rebuilds are not full-history ready.
    ready = through_event_id is None or after >= (next_id if reconstruct or missing_seen else previous_id)
    await apply_candle_batch(db, pair_id=pair_id, expected_event_id=after,
        through_event_id=after, rows=[], history_version=version, history_ready=ready)
    if next_id > high_water and db.bind.dialect.name == 'postgresql':
        from sqlalchemy import text
        await db.execute(text("SELECT setval(pg_get_serial_sequence('fx_pool_event','id'), :value, true)"), {'value': next_id})
    return after


async def _select_pairs(pair_id: Optional[int]) -> list[int]:
    async with async_session_maker() as db:
        stmt = select(FxPair.id).order_by(FxPair.id)
        if pair_id is not None:
            stmt = stmt.where(FxPair.id == pair_id)
        return [int(value) for value in (await db.execute(stmt)).scalars().all()]


def _describe_database() -> str:
    url = make_url(settings.build_db_url())
    database = url.database or ""
    host = url.host or ""
    return f"{url.get_backend_name()}://{host}/{database}"


async def run(
    pair_ids: Sequence[int], through_trade_id: Optional[int], *, dry_run: bool, through_event_id: Optional[int] = None,
) -> int:
    if not await OWNERSHIP.acquire():
        print(
            "拒绝执行：经济写所有权被其他进程持有 "
            f"({OWNERSHIP.reason})；请先停止后端再重建。"
        )
        return 3
    try:
        print(f"数据库：{_describe_database()}；目标 pair：{list(pair_ids)}")
        rebuilt = 0
        for pair_id in pair_ids:
            # Per-pair maintenance GATE, then the pair row lock inside the txn.
            async with GATES.hold(exclusive=[GroupKey("fx", pair_id)]):
                async with async_session_maker() as db:
                    async with db.begin():
                        cutoff = await rebuild_pair(db, pair_id, through_trade_id, through_event_id=through_event_id, dry_run=dry_run)
                if not dry_run:
                    print(f"  pair={pair_id}: 已重建，截止 event_id={cutoff}")
                rebuilt += 1
        print("dry-run 完成：未修改任何派生行" if dry_run else f"完成：重建 {rebuilt} 个 pair")
        return 0
    finally:
        await OWNERSHIP.release()


async def _main(args: argparse.Namespace) -> int:
    through = args.through_trade_id
    pair_ids = await _select_pairs(args.pair_id)
    if not pair_ids:
        print("没有匹配的 FX pair")
        return 0
    if args.dry_run:
        return await run(pair_ids, through, dry_run=True, through_event_id=args.through_event_id)
    if not args.yes:
        answer = input(
            f"将重建 {len(pair_ids)} 个 pair 的派生 K 线（原始成交/资金不变）。"
            "输入 REBUILD 继续: "
        ).strip()
        if answer != "REBUILD":
            print("已取消")
            return 1
    return await run(pair_ids, through, dry_run=False, through_event_id=args.through_event_id)


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pair-id", type=int, help="只重建指定 FX pair")
    parser.add_argument("--all", action="store_true",
                        help="重建全部 pair（含 paused/archived 且保留历史的 pair）")
    parser.add_argument("--through-trade-id", type=int,
                        help="旧参数，明确拒绝；请改用 --through-event-id")
    parser.add_argument("--through-event-id", type=int, help="派生行情截止事件 ID；部分历史不标记 ready")
    parser.add_argument("--dry-run", action="store_true", help="校验固定来源截止和储备链，不写入")
    parser.add_argument("--yes", action="store_true", help="跳过 REBUILD 交互确认")
    args = parser.parse_args(argv)
    if args.pair_id is None and not args.all:
        parser.error("必须指定 --pair-id 或 --all")
    return args


async def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        try:
            return await _main(args)
        except ValueError as exc:
            print(f"拒绝转换：{exc}")
            return 2
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
