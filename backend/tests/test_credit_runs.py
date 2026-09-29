"""WP3 runs：每用户一个 active、动作幂等、round 只在提交时前进、回滚无残留。"""
import sys
import os
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.database import async_session_maker
from app.models.base import User
from app.models.credit import LiquidationAction, LiquidationRun
from app.services.credit.runs import (
    RunStateError,
    close_run,
    find_action_for_round,
    get_or_create_active_run,
    record_action,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


async def _make_user() -> int:
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username="wp3_run_user", casdoor_id="wp3_run_user",
                     cash=Decimal("100"), debt=Decimal("0"))
            s.add(u)
        return int(u.id)


async def test_get_or_create_returns_same_active_run():
    uid = await _make_user()
    async with async_session_maker() as s:
        async with s.begin():
            run1 = await get_or_create_active_run(
                s, user_id=uid, trigger_source="sweep", now=NOW,
            )
            run1_id = int(run1.id)
        async with s.begin():
            run2 = await get_or_create_active_run(
                s, user_id=uid, trigger_source="run_now", now=NOW,
            )
            assert int(run2.id) == run1_id
            assert run2.trigger_source == "sweep"  # 已存在的 run 不被改写
    async with async_session_maker() as s:
        count = (await s.execute(
            select(func.count()).select_from(LiquidationRun).where(
                LiquidationRun.user_id == uid, LiquidationRun.status == "active",
            )
        )).scalar_one()
        assert count == 1


async def test_terminal_run_does_not_block_new_active_run():
    uid = await _make_user()
    async with async_session_maker() as s:
        async with s.begin():
            run = await get_or_create_active_run(
                s, user_id=uid, trigger_source="sweep", now=NOW,
            )
            await close_run(s, run=run, status="recovered", now=NOW)
    async with async_session_maker() as s:
        async with s.begin():
            run2 = await get_or_create_active_run(
                s, user_id=uid, trigger_source="sweep", now=NOW,
            )
            assert run2.status == "active"
    async with async_session_maker() as s:
        total = (await s.execute(
            select(func.count()).select_from(LiquidationRun).where(LiquidationRun.user_id == uid)
        )).scalar_one()
        assert total == 2


async def test_partial_unique_index_blocks_second_active_run():
    uid = await _make_user()
    async with async_session_maker() as s:
        async with s.begin():
            await get_or_create_active_run(s, user_id=uid, trigger_source="sweep", now=NOW)
    async with async_session_maker() as s:
        s.add(LiquidationRun(user_id=uid, status="active", trigger_source="manual",
                             started_at=NOW, updated_at=NOW))
        with pytest.raises(IntegrityError):
            async with s.begin_nested():
                await s.flush()
        await s.rollback()


async def test_record_action_idempotent_and_totals_accumulate():
    uid = await _make_user()
    async with async_session_maker() as s:
        async with s.begin():
            run = await get_or_create_active_run(
                s, user_id=uid, trigger_source="sweep", now=NOW,
            )
            run_id = int(run.id)
            a1 = await record_action(
                s, run=run, round_no=1, kind="sell_group", product="lmsr", group_id=7,
                mode="partial", requested={"pct": "0.1"}, executed={"sold": 3},
                proceeds=Decimal("12.5"), fee=Decimal("0.125"), fee_currency="gold",
                repaid=Decimal("10"), debt_after=Decimal("90"), cash_after=Decimal("2.5"),
                economic_version_after=4,
            )
            a1_id = int(a1.id)
            assert run.rounds == 1 and run.next_round == 2
            # 同 (run, round) 重放：返回既有动作，合计不翻倍
            a1_replay = await record_action(
                s, run=run, round_no=1, kind="repay_only",
                proceeds=Decimal("999"), fee=Decimal("999"),
            )
            assert int(a1_replay.id) == a1_id
            assert a1_replay.kind == "sell_group"
            assert run.rounds == 1 and run.next_round == 2
            assert run.total_proceeds == Decimal("12.5")
            assert run.total_repaid == Decimal("10")
            assert run.total_fee == Decimal("0.125")
            # 新一轮
            await record_action(
                s, run=run, round_no=2, kind="repay_cash",
                proceeds=Decimal("1"), repaid=Decimal("1"), cash_after=Decimal("1.5"),
            )
            assert run.rounds == 2 and run.next_round == 3
            assert run.total_proceeds == Decimal("13.5")
            assert run.total_repaid == Decimal("11")
    async with async_session_maker() as s:
        n = (await s.execute(
            select(func.count()).select_from(LiquidationAction).where(
                LiquidationAction.run_id == run_id,
            )
        )).scalar_one()
        assert n == 2


async def test_record_action_blocked_reason_and_validation():
    uid = await _make_user()
    async with async_session_maker() as s:
        async with s.begin():
            run = await get_or_create_active_run(
                s, user_id=uid, trigger_source="sweep", now=NOW,
            )
            await record_action(
                s, run=run, round_no=1, kind="blocked", product="fx", group_id=2,
                blocked_reason="pair_paused",
            )
            assert run.last_blocked_reason == "pair_paused"
            assert run.last_group_id is None  # 只有 sell_group 更新 last_group
            with pytest.raises(ValueError):
                await record_action(s, run=run, round_no=2, kind="explode")
            with pytest.raises(ValueError):
                await record_action(s, run=run, round_no=2, kind="repay_only",
                                    fee_currency="silver")
            with pytest.raises(ValueError):
                await record_action(s, run=run, round_no=0, kind="repay_only")
            with pytest.raises(ValueError):
                await record_action(s, run=run, round_no=2, kind="repay_only",
                                    product="stock")


async def test_rollback_leaves_no_run_or_action():
    uid = await _make_user()
    async with async_session_maker() as s:
        # 不 commit：显式回滚整个事务，run 与 action 都不落库
        run = await get_or_create_active_run(
            s, user_id=uid, trigger_source="sweep", now=NOW,
        )
        await record_action(s, run=run, round_no=1, kind="repay_only",
                            repaid=Decimal("5"))
        await s.rollback()
    async with async_session_maker() as s:
        runs = (await s.execute(
            select(func.count()).select_from(LiquidationRun).where(LiquidationRun.user_id == uid)
        )).scalar_one()
        actions = (await s.execute(
            select(func.count()).select_from(LiquidationAction).where(LiquidationAction.user_id == uid)
        )).scalar_one()
        assert runs == 0 and actions == 0


async def test_close_run_transitions_and_idempotency():
    uid = await _make_user()
    async with async_session_maker() as s:
        async with s.begin():
            run = await get_or_create_active_run(
                s, user_id=uid, trigger_source="sweep", now=NOW,
            )
            with pytest.raises(RunStateError):
                await close_run(s, run=run, status="active", now=NOW)
            with pytest.raises(ValueError):
                await close_run(s, run=run, status="bogus", now=NOW)
            await close_run(s, run=run, status="insolvent", now=NOW)
            assert run.status == "insolvent"
            assert run.closed_at == NOW
            # 同终态重复 close：幂等 no-op
            await close_run(s, run=run, status="insolvent", now=NOW)
            with pytest.raises(RunStateError):
                await close_run(s, run=run, status="stopped", now=NOW)


async def test_get_or_create_validation():
    uid = await _make_user()
    async with async_session_maker() as s:
        with pytest.raises(ValueError):
            await get_or_create_active_run(s, user_id=uid, trigger_source="", now=NOW)
        with pytest.raises(ValueError):
            await get_or_create_active_run(s, user_id=uid, trigger_source="x" * 33, now=NOW)


async def test_stale_run_object_cannot_overwrite_committed_totals():
    """reviewer blocker 2：S1 持有的旧 run 对象不得把 S2 已提交的 totals/rounds 覆盖掉。"""
    uid = await _make_user()
    async with async_session_maker() as s1:
        async with s1.begin():
            run = await get_or_create_active_run(
                s1, user_id=uid, trigger_source="sweep", now=NOW,
            )
            run_id = int(run.id)
        # s1 的 run 对象停留在 rounds=0 / totals=0（expire_on_commit=False 不刷新）
        assert run.rounds == 0 and run.total_repaid == Decimal("0")

        # 另一个 session 提交了 round 1
        async with async_session_maker() as s2:
            async with s2.begin():
                other = await get_or_create_active_run(
                    s2, user_id=uid, trigger_source="run_now", now=NOW,
                )
                await record_action(s2, run=other, round_no=1, kind="repay_only",
                                    repaid=Decimal("5"))

        # s1 拿着旧对象记录 round 2：必须基于刷新后的 totals=5，而不是旧值 0
        async with s1.begin():
            action = await record_action(
                s1, run=run, round_no=2, kind="repay_only", repaid=Decimal("7"),
            )
            assert int(action.run_id) == run_id
        assert run.rounds == 2 and run.next_round == 3
        assert run.total_repaid == Decimal("12")

    async with async_session_maker() as s:
        row = (await s.execute(
            select(LiquidationRun).where(LiquidationRun.id == run_id)
        )).scalars().one()
        assert row.rounds == 2 and row.total_repaid == Decimal("12")


async def test_stale_run_object_cannot_overwrite_terminal_status():
    """旧快照 status='active' 不能把并发关闭的终态覆盖成别的终态。"""
    uid = await _make_user()
    async with async_session_maker() as s1:
        async with s1.begin():
            run = await get_or_create_active_run(
                s1, user_id=uid, trigger_source="sweep", now=NOW,
            )
            run_id = int(run.id)
        assert run.status == "active"

        async with async_session_maker() as s2:
            async with s2.begin():
                other = await get_or_create_active_run(
                    s2, user_id=uid, trigger_source="run_now", now=NOW,
                )
                await close_run(s2, run=other, status="recovered", now=NOW)

        with pytest.raises(RunStateError):
            async with s1.begin():
                await close_run(s1, run=run, status="insolvent", now=NOW)
        # 同终态重复关闭仍然幂等
        async with s1.begin():
            await close_run(s1, run=run, status="recovered", now=NOW)

    async with async_session_maker() as s:
        row = (await s.execute(
            select(LiquidationRun).where(LiquidationRun.id == run_id)
        )).scalars().one()
        assert row.status == "recovered"


async def test_record_action_refuses_terminal_run_even_with_stale_object():
    uid = await _make_user()
    async with async_session_maker() as s1:
        async with s1.begin():
            run = await get_or_create_active_run(
                s1, user_id=uid, trigger_source="sweep", now=NOW,
            )
            run_id = int(run.id)
        async with async_session_maker() as s2:
            async with s2.begin():
                other = await get_or_create_active_run(
                    s2, user_id=uid, trigger_source="run_now", now=NOW,
                )
                await close_run(s2, run=other, status="stopped", now=NOW)
        with pytest.raises(RunStateError):
            async with s1.begin():
                await record_action(s1, run=run, round_no=1, kind="repay_only")
    async with async_session_maker() as s:
        n = (await s.execute(
            select(func.count()).select_from(LiquidationAction).where(
                LiquidationAction.run_id == run_id,
            )
        )).scalar_one()
        assert n == 0


async def test_find_action_for_round_is_pre_effect_lookup():
    """WP7 接口：效果前查询已有动作；record_action 之后可见，且不产生副作用。"""
    uid = await _make_user()
    async with async_session_maker() as s:
        async with s.begin():
            run = await get_or_create_active_run(
                s, user_id=uid, trigger_source="sweep", now=NOW,
            )
            assert await find_action_for_round(s, run=run, round_no=1) is None
            a1 = await record_action(s, run=run, round_no=1, kind="repay_only",
                                     repaid=Decimal("3"))
            found = await find_action_for_round(s, run=run, round_no=1)
            assert found is not None and int(found.id) == int(a1.id)
            assert await find_action_for_round(s, run=run, round_no=2) is None
            # 重复调用不改变状态
            again = await find_action_for_round(s, run=run, round_no=1)
            assert int(again.id) == int(a1.id)
            assert run.rounds == 1 and run.total_repaid == Decimal("3")
        with pytest.raises(ValueError):
            await find_action_for_round(s, run=run, round_no=0)
