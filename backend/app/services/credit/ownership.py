"""单经济写实例所有权（spec §6.1；计划 WP3 "ownership"）。

契约：

- PostgreSQL 用 **session 级** ``pg_try_advisory_lock(bigint)``，持有在**专用非池化
  连接**上；不是池化连接（池化连接被回收会把锁带走），也不是有 TTL 的租约。
- 专用连接用 ``isolation_level="AUTOCOMMIT"``：advisory lock 是 session 级的，不需要
  事务；否则每次 ``SELECT`` 都会开一个长事务，连接永远 ``idle in transaction`` 并
  pin 住 ``backend_xmin``（reviewer blocker 3，PG 探针已复现）。
- 连接丢失 / 锁丢失 → ``writes_enabled=False`` + CRITICAL，之后所有经济写入口
  必须通过 ``require_writes()`` 被拒绝；进程需重启（或显式 ``release()`` 后重新
  ``acquire()``）才能恢复写权限。不做自动重抢，避免两个实例来回抢锁。
- ``所有经济写实例`` 的实例启动时必须持锁：``acquire(required=True)``
  拿不到锁直接抛 ``OwnershipError``，让启动失败（读实例可继续但不写）。
- 非 PostgreSQL（SQLite 开发/测试）没有 advisory lock：``supported=False``，
  单进程语义下 ``is_owner=True`` / ``writes_enabled=True``，行为与改造前一致。
- ``read_only`` 实例（``THCCB_READ_ONLY_INSTANCE``）不获取锁、不写库；main.py 在
  ``init_db`` **之前**就用它跳过全部启动写入。

WP6/WP7 的经济写入口在拿到用户锁后必须调用 ``OWNERSHIP.require_writes()``。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings

logger = logging.getLogger("thccb.credit.ownership")

#: 全局唯一锁键（bigint，落在 int64 内；ASCII "THCCB_CR"）。
LOCK_KEY = int.from_bytes(b"THCCB_CR", "big")
#: 连接/锁存活探测间隔（秒）。
DEFAULT_PING_INTERVAL = 5.0


class OwnershipError(RuntimeError):
    """需要所有权却拿不到（所有经济写实例 时必须启动失败）。"""


class EconomicWritesDisabled(RuntimeError):
    """本进程当前不允许任何经济写入（只读实例 / 锁丢失 / 非 owner）。"""


@dataclass(frozen=True)
class OwnershipState:
    supported: bool
    is_owner: bool
    writes_enabled: bool
    reason: Optional[str]


class WriteOwnership:
    """单写所有权门（进程级单例 ``OWNERSHIP``）。"""

    def __init__(
        self,
        *,
        url: Optional[str] = None,
        lock_key: int = LOCK_KEY,
        ping_interval: float = DEFAULT_PING_INTERVAL,
    ) -> None:
        self._url = str(url or settings.build_db_url())
        self._backend = make_url(self._url).get_backend_name()
        self._lock_key = int(lock_key)
        self._ping_interval = max(0.05, float(ping_interval))
        self._engine: Optional[AsyncEngine] = None
        self._conn: Optional[AsyncConnection] = None
        self._heartbeat: Optional[asyncio.Task] = None
        self._is_owner = False
        self._writes_enabled = False
        self._reason: Optional[str] = None
        self._read_only = False

    # ── 只读状态 ────────────────────────────────────────────────────────
    @property
    def supported(self) -> bool:
        return self._backend == "postgresql"

    @property
    def is_owner(self) -> bool:
        return self._is_owner

    @property
    def writes_enabled(self) -> bool:
        return self._writes_enabled

    @property
    def reason(self) -> Optional[str]:
        return self._reason

    @property
    def connection(self) -> Optional[AsyncConnection]:
        """专用连接（诊断/测试观察用；调用方不得拿它做业务查询）。"""
        return self._conn

    def state(self) -> OwnershipState:
        return OwnershipState(
            supported=self.supported,
            is_owner=self._is_owner,
            writes_enabled=self._writes_enabled,
            reason=self._reason,
        )

    # ── 生命周期 ────────────────────────────────────────────────────────
    async def acquire(
        self,
        *,
        required: bool = False,
        read_only: bool = False,
    ) -> bool:
        """获取所有权；返回是否持有。``required=True`` 且拿不到时抛 OwnershipError。"""
        if self._is_owner:
            return True
        if read_only or self._read_only:
            self.mark_read_only()
            return False

        if not self.supported:
            self._is_owner = True
            self._writes_enabled = True
            self._reason = "ownership_not_enforced_non_postgresql"
            logger.warning(
                "ownership: 数据库方言 %s 无 advisory lock，单进程语义下不启用所有权保护",
                self._backend,
            )
            return True

        # AUTOCOMMIT：advisory lock 与探测语句都不需要事务，避免 idle-in-transaction
        # 长事务 pin xmin（reviewer blocker 3）。
        engine = create_async_engine(
            self._url, poolclass=NullPool, isolation_level="AUTOCOMMIT",
        )
        try:
            conn = await engine.connect()
        except Exception as exc:
            await engine.dispose()
            self._reason = f"ownership_connect_failed: {type(exc).__name__}"
            logger.critical("ownership: 专用连接建立失败（%s）", self._reason)
            if required:
                raise OwnershipError(self._reason) from exc
            return False

        acquired = bool(
            (await conn.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": self._lock_key},
            )).scalar()
        )
        if not acquired:
            await conn.close()
            await engine.dispose()
            self._is_owner = False
            self._writes_enabled = False
            self._reason = "advisory_lock_held_by_other_instance"
            logger.critical(
                "ownership: 另一个实例持有经济写锁（key=%s）；本进程禁止经济写入",
                self._lock_key,
            )
            if required:
                raise OwnershipError(self._reason)
            return False

        self._engine = engine
        self._conn = conn
        self._is_owner = True
        self._writes_enabled = True
        self._reason = None
        self._heartbeat = asyncio.create_task(self._heartbeat_loop())
        logger.info(
            "ownership: 已持有经济写锁 key=%s（专用非池化连接，pid 级 session lock）",
            self._lock_key,
        )
        return True

    def mark_read_only(self) -> None:
        """只读实例：不获取锁、不允许经济写入。"""
        self._read_only = True
        self._is_owner = False
        self._writes_enabled = False
        self._reason = "read_only_instance"

    async def release(self) -> None:
        """释放锁与专用连接（幂等）。释放后本进程不再允许经济写入。"""
        await self._stop_heartbeat()
        conn, engine = self._conn, self._engine
        self._conn = None
        self._engine = None
        if conn is not None:
            try:
                await conn.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": self._lock_key},
                )
            except Exception as exc:  # 连接已死时锁已随 session 消失，无需补救
                logger.warning("ownership: unlock 失败（按已释放处理）: %r", exc)
            try:
                await conn.close()
            except Exception:
                pass
        if engine is not None:
            await engine.dispose()
        self._is_owner = False
        self._writes_enabled = False
        if not self._read_only:
            self._reason = "released"

    def require_writes(self) -> None:
        """经济写入口守卫；不允许写时抛 ``EconomicWritesDisabled``。"""
        if not self._writes_enabled:
            raise EconomicWritesDisabled(
                f"本进程经济写入已禁用: {self._reason or 'unknown'}"
            )

    # ── 存活探测 ────────────────────────────────────────────────────────
    async def _heartbeat_loop(self) -> None:
        """定期确认专用连接仍在且 advisory lock 仍属于本 session。"""
        try:
            while True:
                await asyncio.sleep(self._ping_interval)
                conn = self._conn
                if conn is None:
                    return
                try:
                    row = (await conn.execute(
                        text(
                            "SELECT 1 FROM pg_locks "
                            "WHERE locktype = 'advisory' AND pid = pg_backend_pid() "
                            "AND classid = :high AND objid = :low"
                        ),
                        {
                            "high": (self._lock_key >> 32) & 0xFFFFFFFF,
                            "low": self._lock_key & 0xFFFFFFFF,
                        },
                    )).first()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._lose(f"heartbeat_failed:{type(exc).__name__}")
                    return
                if row is None:
                    self._lose("advisory_lock_not_held")
                    return
        except asyncio.CancelledError:
            raise

    def _lose(self, reason: str) -> None:
        if not self._is_owner and not self._writes_enabled:
            return
        self._is_owner = False
        self._writes_enabled = False
        self._reason = reason
        logger.critical(
            "ownership: 经济写所有权丢失（%s）——立即禁止后续经济写入；"
            "需重启进程或显式 release()+acquire() 才能恢复",
            reason,
        )

    async def _stop_heartbeat(self) -> None:
        task = self._heartbeat
        self._heartbeat = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


#: 进程级单例：main.py 启动时 acquire，WP6/WP7 写入口用 require_writes()。
OWNERSHIP = WriteOwnership()
