"""按品种读写门闩（计划 §3.2 冻结签名；spec §6.2 第 2/4 条）。

纪律（调用方必须遵守，违反即 fail-fast）：

- **单批全序**：一次 ``hold()`` 给出本次操作涉及的**全部**品种；重复键合并为独占；
  内部按 ``(product, group_id)`` 升序登记，逆序释放。禁止拿到一部分门闩后再去
  补拿另一个（会形成环）。同一 task 在已持门闩时再次 ``hold()`` 直接抛
  ``GateOrderError``（含 shared → exclusive 的升级）。
- **全有或全无**：等待期间不持有任何门闩、不占 DB 连接、不发 writer 命令；
  要么整批拿到，要么一个都不拿。
- **取消安全**：状态迁移（授予 / 释放 / 出队）全部是**同步**代码，没有任何 await
  窗口；拿到门闩后被取消 / 抛异常时在 ``finally`` 里同步释放整批。反复 cancel
  也不会漏放（reviewer blocker 1：旧实现 release 要 await Condition，二次取消
  会永久泄漏键）。
- **公平（reviewer blocker 4）**：FIFO + 写者优先。新请求只在不与任何**在队**
  请求的键集相交、且键全空闲时才可插队；在队的独占等待者不会被后到的共享请求
  饿死（后到者必须排在它后面）。互不相交的品种仍可并发通过（spec §6.1）。

与 WP6 的配合：门闩必须在 DB 事务**之外**获取；commit 后仍持门闩做完无 IO 的
镜像更新再释放。本模块只有内存状态，不做任何 IO。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Iterable, Sequence

from app.services.credit.keys import GroupKey


class GateOrderError(RuntimeError):
    """调用方违反门闩纪律：重入 / 升级 / 分批补拿。"""


@dataclass
class _Request:
    keys: tuple[GroupKey, ...]                  # 升序，全部涉及的品种
    key_set: frozenset[GroupKey]
    exclusive: frozenset[GroupKey]
    shared: frozenset[GroupKey]
    future: "asyncio.Future[None]"
    task: "asyncio.Task | None" = None
    granted: bool = False


@dataclass
class GateMetrics:
    """轻量指标（WP7 消费；只读快照）。"""

    waiting: int = 0
    holders: int = 0
    held_keys: int = 0


class SymbolGateSet:
    """进程内按品种的共享/独占门闩集合。

    进程内必须**只有一个实例**（全局单例 ``GATES``）：两个实例互不可见，
    会绕过互斥。WP6/WP7 的所有经济写路径共用它。

    内部状态全部在事件循环线程内同步迁移（无 await 的检查-授予-出队临界区），
    因此释放路径不需要抢任何锁，取消安全。
    """

    def __init__(self) -> None:
        self._queue: list[_Request] = []        # FIFO 等待队列（可能含已授予未唤醒项）
        self._exclusive: set[GroupKey] = set()
        self._shared: dict[GroupKey, int] = {}
        # task -> 该 task 当前持有的全部键（重入/升级检测与取消释放）
        self._task_keys: dict["asyncio.Task", frozenset[GroupKey]] = {}

    # ── 公开接口（计划 §3.2 冻结）────────────────────────────────────────
    @asynccontextmanager
    async def hold(
        self,
        *,
        exclusive: Sequence[GroupKey] = (),
        shared: Sequence[GroupKey] = (),
    ) -> AsyncIterator[None]:
        """整批获取门闩；``async with`` 退出时逆序释放。

        ``exclusive`` / ``shared`` 中的重复键合并为独占（独占优先）。
        空批次是合法的 no-op（无门闩可持）。
        """
        task = asyncio.current_task()
        keys, exclusive_set = _normalize(exclusive, shared)
        self._assert_not_reentrant(task, keys)
        if not keys:
            yield
            return

        request = _Request(
            keys=keys,
            key_set=frozenset(keys),
            exclusive=exclusive_set,
            shared=frozenset(k for k in keys if k not in exclusive_set),
            future=asyncio.get_running_loop().create_future(),
            task=task,
        )
        try:
            if not (self._may_jump_queue(request) and self._try_grant(request)):
                self._queue.append(request)
                while not request.granted:
                    await request.future
            yield
        finally:
            # 同步清理：授予则释放整批，未授予则出队并唤醒后续等待者。
            # 无 await ⇒ 反复取消也无法打断，不会泄漏键。
            self._abort(request)

    def held_keys(self) -> frozenset[GroupKey]:
        """当前进程内被任何 task 持有的键（指标用）。"""
        return frozenset(self._exclusive) | frozenset(self._shared)

    def held_keys_by_current_task(self) -> frozenset[GroupKey]:
        """当前 task 持有的键（测试/自检用）。"""
        task = asyncio.current_task()
        if task is None:
            return frozenset()
        return self._task_keys.get(task, frozenset())

    def waiting_count(self) -> int:
        return len([item for item in self._queue if not item.granted])

    def metrics(self) -> GateMetrics:
        return GateMetrics(
            waiting=self.waiting_count(),
            holders=len(self._task_keys),
            held_keys=len(self.held_keys()),
        )

    # ── 内部（全部同步，无 await）───────────────────────────────────────
    def _assert_not_reentrant(
        self, task: "asyncio.Task | None", keys: tuple[GroupKey, ...],
    ) -> None:
        if task is None:
            return
        held = self._task_keys.get(task)
        if held:
            raise GateOrderError(
                "已持有门闩时禁止再次 hold（禁止分批补拿/升级）: "
                f"held={sorted(held)} requested={sorted(keys)}"
            )

    def _may_jump_queue(self, request: _Request) -> bool:
        """FIFO + 写者优先：与任何在队请求键集相交就不许插队。"""
        for queued in self._queue:
            if queued.granted:
                continue
            if request.key_set & queued.key_set:
                return False
        return True

    def _try_grant(self, request: _Request) -> bool:
        """所需键全部空闲则整批登记并返回 True（同步，无 await）。"""
        for key in request.exclusive:
            if key in self._exclusive or self._shared.get(key, 0):
                return False
        for key in request.shared:
            if key in self._exclusive:
                return False

        for key in request.keys:
            if key in request.exclusive:
                self._exclusive.add(key)
            else:
                self._shared[key] = self._shared.get(key, 0) + 1
        if request.task is not None:
            self._task_keys[request.task] = frozenset(request.keys)
        request.granted = True
        return True

    def _wake_waiters(self) -> None:
        """按 FIFO 顺序唤醒：后来的请求不得越过在与队请求上冲突的更早请求。"""
        if not self._queue:
            return
        blocked: set[GroupKey] = set()
        remaining: list[_Request] = []
        for request in self._queue:
            if request.granted:
                continue
            if request.future.cancelled():
                # 任务已取消但尚未跑到 finally：视为放弃，不授予、不挡后面
                continue
            if request.key_set & blocked:
                remaining.append(request)
                blocked |= request.key_set
                continue
            if self._try_grant(request):
                if not request.future.done():
                    request.future.set_result(None)
                else:  # 理论上不可达（cancelled 已排除）；保险起见立即释放
                    self._release(request)
            else:
                remaining.append(request)
                blocked |= request.key_set
        self._queue = remaining

    def _drop_waiter(self, request: _Request) -> None:
        try:
            self._queue.remove(request)
        except ValueError:
            pass

    def _abort(self, request: _Request) -> None:
        """同步清理（幂等）：已授予→释放；未授予→出队并唤醒后续。"""
        if request.granted:
            self._release(request)
            return
        self._drop_waiter(request)
        self._wake_waiters()

    def _release(self, request: _Request) -> None:
        if not request.granted:
            return
        request.granted = False
        for key in reversed(request.keys):
            if key in request.exclusive:
                self._exclusive.discard(key)
            else:
                count = self._shared.get(key, 0) - 1
                if count <= 0:
                    self._shared.pop(key, None)
                else:
                    self._shared[key] = count
        if request.task is not None:
            self._task_keys.pop(request.task, None)
        self._wake_waiters()


def _normalize(
    exclusive: Iterable[GroupKey],
    shared: Iterable[GroupKey],
) -> tuple[tuple[GroupKey, ...], frozenset[GroupKey]]:
    """重复键合并为独占；返回 ``(升序全部键, 独占集合)``。"""
    exclusive_set = set(exclusive)
    all_keys = exclusive_set | set(shared)
    for key in all_keys:
        if not isinstance(key, GroupKey):
            raise TypeError(f"门闩键必须是 GroupKey: {key!r}")
    ordered = tuple(sorted(all_keys))
    return ordered, frozenset(exclusive_set)


#: 进程级单例：所有经济写路径必须共用（见类 docstring）。
GATES = SymbolGateSet()
