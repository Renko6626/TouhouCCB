"""WP3 runs 的 PG 并发幂等：并发建 run 只留一个 active；并发同 round 动作只落一条。

跑法：``TEST_PG_DATABASE_URL=postgresql+asyncpg://.../credit_wp3_test pytest -m pg tests/pg/test_pg_credit_runs.py``
每个测试由 ``pg_engine`` fixture drop_all/create_all 隔离。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.base import User
from app.models.credit import LiquidationAction, LiquidationRun
from app.services.credit.runs import (
    RunStateError,
    close_run,
    find_action_for_round,
    get_or_create_active_run,
    record_action,
)

pytestmark = [pytest.mark.pg, pytest.mark.asyncio]

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


async def _make_user(pg_sessionmaker) -> int:
    async with pg_sessionmaker() as s:
        async with s.begin():
            u = User(username="wp3_pg_run", casdoor_id="wp3_pg_run",
                     cash=Decimal("100"), debt=Decimal("50"))
            s.add(u)
        return int(u.id)


async def test_pg_concurrent_get_or_create_yields_one_active_run(pg_sessionmaker):
    uid = await _make_user(pg_sessionmaker)

    async def create(trigger: str) -> int:
        async with pg_sessionmaker() as s:
            async with s.begin():
                run = await get_or_create_active_run(
                    s, user_id=uid, trigger_source=trigger, now=NOW,
                )
                return int(run.id)

    ids = await asyncio.gather(*(create(f"t{i}") for i in range(4)))
    assert len(set(ids)) == 1, f"并发创建出现多个 run id: {ids}"

    async with pg_sessionmaker() as s:
        rows = (await s.execute(
            select(func.count()).select_from(LiquidationRun).where(
                LiquidationRun.user_id == uid, LiquidationRun.status == "active",
            )
        )).scalar_one()
        assert rows == 1


async def test_pg_concurrent_record_action_is_idempotent(pg_sessionmaker):
    uid = await _make_user(pg_sessionmaker)
    async with pg_sessionmaker() as s:
        async with s.begin():
            run = await get_or_create_active_run(
                s, user_id=uid, trigger_source="sweep", now=NOW,
            )
            run_id = int(run.id)

    async def record() -> int:
        async with pg_sessionmaker() as s:
            async with s.begin():
                row = (await s.execute(
                    select(LiquidationRun).where(LiquidationRun.id == run_id)
                )).scalars().one()
                action = await record_action(
                    s, run=row, round_no=1, kind="repay_only",
                    repaid=Decimal("5"), debt_after=Decimal("45"),
                    cash_after=Decimal("100"), economic_version_after=1,
                )
                return int(action.id)

    ids = await asyncio.gather(record(), record(), record())
    assert len(set(ids)) == 1, f"并发重放产生多条动作: {ids}"

    async with pg_sessionmaker() as s:
        n = (await s.execute(
            select(func.count()).select_from(LiquidationAction).where(
                LiquidationAction.run_id == run_id,
            )
        )).scalar_one()
        assert n == 1
        row = (await s.execute(
            select(LiquidationRun).where(LiquidationRun.id == run_id)
        )).scalars().one()
        # 合计只累计一次，round 只前进一次
        assert row.rounds == 1 and row.next_round == 2
        assert row.total_repaid == Decimal("5")
        assert row.total_proceeds == Decimal("0")


async def test_pg_close_run_is_single_transition(pg_sessionmaker):
    uid = await _make_user(pg_sessionmaker)
    async with pg_sessionmaker() as s:
        async with s.begin():
            run = await get_or_create_active_run(
                s, user_id=uid, trigger_source="sweep", now=NOW,
            )
            run_id = int(run.id)
            await close_run(s, run=run, status="recovered", now=NOW)

    async def close_again(status: str):
        async with pg_sessionmaker() as s:
            async with s.begin():
                row = (await s.execute(
                    select(LiquidationRun).where(LiquidationRun.id == run_id)
                )).scalars().one()
                await close_run(s, run=row, status=status, now=NOW)

    # 同终态重复关闭幂等；换终态必须拒绝
    await close_again("recovered")
    with pytest.raises(RunStateError):
        await close_again("stopped")


async def test_pg_stale_run_object_cannot_overwrite_committed_totals(pg_sessionmaker):
    """真 PG 行锁 + populate_existing：S1 旧对象不得覆盖 S2 已提交的 totals/rounds。"""
    uid = await _make_user(pg_sessionmaker)
    async with pg_sessionmaker() as s1:
        async with s1.begin():
            run = await get_or_create_active_run(
                s1, user_id=uid, trigger_source="sweep", now=NOW,
            )
            run_id = int(run.id)
        assert run.rounds == 0 and run.total_repaid == Decimal("0")   # 旧快照

        async with pg_sessionmaker() as s2:
            async with s2.begin():
                other = await get_or_create_active_run(
                    s2, user_id=uid, trigger_source="run_now", now=NOW,
                )
                await record_action(s2, run=other, round_no=1, kind="repay_only",
                                    repaid=Decimal("5"))

        async with s1.begin():
            action = await record_action(
                s1, run=run, round_no=2, kind="repay_only", repaid=Decimal("7"),
            )
            assert int(action.run_id) == run_id
        assert run.rounds == 2 and run.next_round == 3
        assert run.total_repaid == Decimal("12")

    async with pg_sessionmaker() as s:
        row = (await s.execute(
            select(LiquidationRun).where(LiquidationRun.id == run_id)
        )).scalars().one()
        assert row.rounds == 2 and row.next_round == 3
        assert row.total_repaid == Decimal("12")


async def test_pg_stale_run_object_cannot_overwrite_terminal(pg_sessionmaker):
    uid = await _make_user(pg_sessionmaker)
    async with pg_sessionmaker() as s1:
        async with s1.begin():
            run = await get_or_create_active_run(
                s1, user_id=uid, trigger_source="sweep", now=NOW,
            )
            run_id = int(run.id)
        assert run.status == "active"

        async with pg_sessionmaker() as s2:
            async with s2.begin():
                other = await get_or_create_active_run(
                    s2, user_id=uid, trigger_source="run_now", now=NOW,
                )
                await close_run(s2, run=other, status="recovered", now=NOW)

        with pytest.raises(RunStateError):
            async with s1.begin():
                await close_run(s1, run=run, status="insolvent", now=NOW)
        with pytest.raises(RunStateError):
            async with s1.begin():
                await record_action(s1, run=run, round_no=1, kind="repay_only")

    async with pg_sessionmaker() as s:
        row = (await s.execute(
            select(LiquidationRun).where(LiquidationRun.id == run_id)
        )).scalars().one()
        assert row.status == "recovered"
        assert await find_action_for_round(s, run=row, round_no=1) is None
        n = (await s.execute(
            select(func.count()).select_from(LiquidationAction).where(
                LiquidationAction.run_id == run_id,
            )
        )).scalar_one()
        assert n == 0
