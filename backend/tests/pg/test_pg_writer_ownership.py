"""WP3 PostgreSQL 所有权：第二实例被拒、锁丢失停写、断连后可重新获取。

跑法：``TEST_PG_DATABASE_URL=postgresql+asyncpg://.../credit_wp3_test pytest -m pg tests/pg/test_pg_writer_ownership.py``
专用库由父进程创建；本文件不做 drop/create（所有权只用到 advisory lock + pg_locks）。
"""
from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import text

from app.services.credit.ownership import (
    LOCK_KEY,
    EconomicWritesDisabled,
    OwnershipError,
    WriteOwnership,
)

pytestmark = [pytest.mark.pg, pytest.mark.asyncio]


def _url() -> str:
    return os.environ["TEST_PG_DATABASE_URL"]


async def test_single_owner_second_instance_rejected(pg_engine):
    first = WriteOwnership(url=_url())
    second = WriteOwnership(url=_url())
    try:
        assert await first.acquire(required=True) is True
        assert first.is_owner and first.writes_enabled
        first.require_writes()

        assert await second.acquire() is False
        assert second.is_owner is False
        assert second.writes_enabled is False
        assert second.reason == "advisory_lock_held_by_other_instance"
        with pytest.raises(EconomicWritesDisabled):
            second.require_writes()
        with pytest.raises(OwnershipError):
            await second.acquire(required=True)  # unified_credit_enabled=true 的启动路径
    finally:
        await second.release()
        await first.release()


async def test_release_allows_reacquire(pg_engine):
    first = WriteOwnership(url=_url())
    second = WriteOwnership(url=_url())
    try:
        assert await first.acquire() is True
        assert await second.acquire() is False
        await first.release()
        assert first.writes_enabled is False
        # 释放后另一个实例可以拿到（部署切换路径）
        assert await second.acquire() is True
        assert second.writes_enabled is True
    finally:
        await second.release()
        await first.release()


async def test_connection_loss_disables_writes_and_frees_lock(pg_engine):
    first = WriteOwnership(url=_url(), ping_interval=0.05)
    second = WriteOwnership(url=_url())
    try:
        assert await first.acquire() is True
        assert first.connection is not None
        await first.connection.close()  # 模拟专用连接断开

        await asyncio.sleep(0.3)
        assert first.writes_enabled is False
        assert first.is_owner is False
        assert first.reason is not None and first.reason.startswith("heartbeat_failed")
        with pytest.raises(EconomicWritesDisabled):
            first.require_writes()

        # 连接断开 → PG 自动释放 session advisory lock → 第二实例可接管
        assert await second.acquire() is True
    finally:
        await second.release()
        await first.release()


async def test_lock_loss_without_connection_loss_is_detected(pg_engine):
    own = WriteOwnership(url=_url(), ping_interval=0.05)
    other = WriteOwnership(url=_url())
    try:
        assert await own.acquire() is True
        # 连接活着但锁被显式释放（例如误操作/人工解锁）
        await own.connection.execute(
            text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY},
        )
        await asyncio.sleep(0.3)
        assert own.writes_enabled is False
        assert own.reason == "advisory_lock_not_held"
        assert await other.acquire() is True
    finally:
        await other.release()
        await own.release()


async def _assert_connection_not_idle_in_transaction(pg_engine, pid: int) -> None:
    """从**另一个**连接观察：专用连接不得停在 idle in transaction / pin xmin。"""
    async with pg_engine.connect() as monitor:
        row = (await monitor.execute(
            text("SELECT state, backend_xmin FROM pg_stat_activity WHERE pid = :pid"),
            {"pid": pid},
        )).first()
    assert row is not None
    state, backend_xmin = row
    assert state not in ("idle in transaction", "idle in transaction (aborted)"), (state, backend_xmin)
    assert backend_xmin is None, (state, backend_xmin)


async def test_pg_ownership_connection_never_idle_in_transaction(pg_engine):
    """reviewer blocker 3：acquire 与心跳之后都不得留下长事务（xmin 不得被 pin）。"""
    own = WriteOwnership(url=_url(), ping_interval=0.05)
    try:
        assert await own.acquire() is True
        pid = int((await own.connection.execute(
            text("SELECT pg_backend_pid()")
        )).scalar())
        await _assert_connection_not_idle_in_transaction(pg_engine, pid)

        await asyncio.sleep(0.2)          # 至少跑过 3 次心跳
        assert own.writes_enabled is True
        await _assert_connection_not_idle_in_transaction(pg_engine, pid)

        # 心跳期间连接仍然持有 advisory lock（AUTOCOMMIT 不影响 session lock）
        held = (await own.connection.execute(
            text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                "AND pid = pg_backend_pid() AND classid = :high AND objid = :low"
            ),
            {"high": (LOCK_KEY >> 32) & 0xFFFFFFFF, "low": LOCK_KEY & 0xFFFFFFFF},
        )).scalar()
        assert int(held) == 1
    finally:
        await own.release()
