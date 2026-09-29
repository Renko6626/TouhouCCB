"""WP3 ownership：非 PG 方言降级、只读实例、require_writes 守卫（SQLite 侧）。"""
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from app.services.credit.ownership import (
    OWNERSHIP,
    EconomicWritesDisabled,
    WriteOwnership,
)

pytestmark = pytest.mark.asyncio

SQLITE_URL = "sqlite+aiosqlite:////dev/shm/credit-wp3.db"


async def test_non_postgresql_is_single_process_compatible():
    own = WriteOwnership(url=SQLITE_URL)
    assert own.supported is False
    assert await own.acquire() is True
    assert own.is_owner is True
    assert own.writes_enabled is True
    assert own.reason == "ownership_not_enforced_non_postgresql"
    own.require_writes()  # 不抛
    st = own.state()
    assert st.supported is False and st.writes_enabled is True

    await own.release()
    assert own.is_owner is False
    assert own.writes_enabled is False
    with pytest.raises(EconomicWritesDisabled):
        own.require_writes()

    # 释放后可重新获取（重启恢复路径）
    assert await own.acquire() is True
    await own.release()


async def test_read_only_instance_never_writes():
    own = WriteOwnership(url=SQLITE_URL)
    assert await own.acquire(read_only=True) is False
    assert own.is_owner is False
    assert own.writes_enabled is False
    assert own.reason == "read_only_instance"
    with pytest.raises(EconomicWritesDisabled):
        own.require_writes()
    # read_only 粘性：再次 acquire 也不放行
    assert await own.acquire() is False
    assert own.writes_enabled is False
    await own.release()
    assert own.reason == "read_only_instance"


async def test_mark_read_only_after_acquire_disables_writes():
    own = WriteOwnership(url=SQLITE_URL)
    await own.acquire()
    own.mark_read_only()
    assert own.writes_enabled is False
    with pytest.raises(EconomicWritesDisabled):
        own.require_writes()
    await own.release()


async def test_process_singleton_exists():
    assert isinstance(OWNERSHIP, WriteOwnership)
