"""WP3 门闩：全序批量获取、共享/独占、取消释放、无死锁（计划 WP3 包内验收）。

纯内存测试，不碰 DB。真并发竞争用 asyncio.gather 模拟。
"""
import asyncio
import random
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from app.services.credit.gates import GATES, GateOrderError, SymbolGateSet
from app.services.credit.keys import GroupKey

pytestmark = pytest.mark.asyncio

N_GROUPS = 200


def _key(i: int, product: str = "lmsr") -> GroupKey:
    return GroupKey(product if product in ("lmsr", "fx") else "lmsr", i)


async def test_exclusive_blocks_shared_and_exclusive():
    gates = SymbolGateSet()
    k = _key(1)
    entered = asyncio.Event()
    release = asyncio.Event()
    conflicts: list[str] = []

    async def holder():
        async with gates.hold(exclusive=[k]):
            entered.set()
            await release.wait()

    async def other():
        await entered.wait()
        async with gates.hold(shared=[k]):
            if gates.held_keys() != frozenset({k}):
                conflicts.append("shared entered while exclusive held")

    async def other_exclusive():
        await entered.wait()
        async with gates.hold(exclusive=[k]):
            if gates.held_keys() != frozenset({k}):
                conflicts.append("exclusive entered while exclusive held")

    h = asyncio.create_task(holder())
    await entered.wait()
    o1 = asyncio.create_task(other())
    o2 = asyncio.create_task(other_exclusive())
    await asyncio.sleep(0.01)  # 让两个等待者挂到队列上
    assert gates.waiting_count() == 2
    release.set()
    await asyncio.wait_for(asyncio.gather(h, o1, o2), timeout=5)
    assert conflicts == []
    assert gates.held_keys() == frozenset()


async def test_shared_holders_coexist_and_unrelated_key_not_blocked():
    gates = SymbolGateSet()
    k1, k2 = _key(1), _key(2)
    both_in = asyncio.Event()
    counter = {"in": 0, "max": 0}

    async def sharer():
        async with gates.hold(shared=[k1]):
            counter["in"] += 1
            counter["max"] = max(counter["max"], counter["in"])
            if counter["in"] == 2:
                both_in.set()
            await both_in.wait()
            counter["in"] -= 1

    async def unrelated():
        await both_in.wait()
        # k1 被两个共享持有者占着，但 k2 空闲：无关品种必须立刻拿到
        async with gates.hold(exclusive=[k2]):
            assert k2 in gates.held_keys()

    t1 = asyncio.create_task(sharer())
    t2 = asyncio.create_task(sharer())
    t3 = asyncio.create_task(unrelated())
    await asyncio.wait_for(asyncio.gather(t1, t2, t3), timeout=5)
    assert counter["max"] == 2


async def test_duplicate_key_merges_to_exclusive():
    gates = SymbolGateSet()
    k = _key(3)
    entered = asyncio.Event()
    release = asyncio.Event()
    blocked_shared = {"entered": False}

    async def holder():
        async with gates.hold(exclusive=[k], shared=[k]):
            entered.set()
            await release.wait()

    async def sharer():
        await entered.wait()
        async with gates.hold(shared=[k]):
            blocked_shared["entered"] = True

    h = asyncio.create_task(holder())
    await entered.wait()
    s = asyncio.create_task(sharer())
    await asyncio.sleep(0.01)
    assert blocked_shared["entered"] is False
    release.set()
    await asyncio.wait_for(asyncio.gather(h, s), timeout=5)
    assert blocked_shared["entered"] is True


async def test_reentrancy_and_shared_to_exclusive_upgrade_rejected():
    gates = SymbolGateSet()
    k1, k2 = _key(1), _key(2)
    async with gates.hold(shared=[k1]):
        with pytest.raises(GateOrderError):
            async with gates.hold(exclusive=[k1]):
                pass
        with pytest.raises(GateOrderError):
            async with gates.hold(exclusive=[k2]):
                pass
    assert gates.held_keys() == frozenset()
    # 释放后可以再次获取（重入检测不残留状态）
    async with gates.hold(exclusive=[k1]):
        assert gates.held_keys_by_current_task() == frozenset({k1})


async def test_waiting_cancellation_leaves_no_partial_hold():
    gates = SymbolGateSet()
    k1, k2 = _key(1), _key(2)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with gates.hold(exclusive=[k1]):
            entered.set()
            await release.wait()

    async def waiter():
        async with gates.hold(exclusive=[k1, k2]):
            raise AssertionError("不应拿到（k1 被独占）")

    h = asyncio.create_task(holder())
    await entered.wait()
    w = asyncio.create_task(waiter())
    await asyncio.sleep(0.01)
    assert gates.waiting_count() == 1
    w.cancel()
    with pytest.raises(asyncio.CancelledError):
        await w
    # 取消后没有半批持有：k2 必须立刻可独占（k1 仍被 holder 占）
    assert gates.held_keys() == frozenset({k1})
    async with gates.hold(exclusive=[k2]):
        pass
    release.set()
    await asyncio.wait_for(h, timeout=5)
    assert gates.held_keys() == frozenset()


async def test_body_cancellation_releases_whole_batch():
    gates = SymbolGateSet()
    k1, k2 = _key(1), _key(2)
    entered = asyncio.Event()

    async def victim():
        async with gates.hold(exclusive=[k1, k2]):
            entered.set()
            await asyncio.Event().wait()  # 永久等待 → 被取消

    t = asyncio.create_task(victim())
    await entered.wait()
    assert gates.held_keys() == frozenset({k1, k2})
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert gates.held_keys() == frozenset()
    assert gates.held_keys_by_current_task() == frozenset()


async def test_empty_hold_is_noop():
    gates = SymbolGateSet()
    async with gates.hold(exclusive=[], shared=[]):
        assert gates.held_keys() == frozenset()
    assert gates.metrics().waiting == 0


@pytest.mark.parametrize("round_no", range(20))
async def test_random_overlap_200_groups_no_conflict_no_deadlock(round_no):
    """200 组随机重叠、20 轮重跑：互斥不变式成立且无死锁（计划 WP3 验收）。"""
    gates = SymbolGateSet()
    rng = random.Random(1000 + round_no)
    all_keys = [_key(i + 1) for i in range(N_GROUPS)]
    holders: dict[GroupKey, list[tuple[int, str]]] = {}
    violations: list[str] = []

    async def worker(wid: int):
        for _ in range(6):
            size = rng.randint(1, 6)
            picked = rng.sample(all_keys, size)
            exclusive = [k for k in picked if rng.random() < 0.35]
            shared = [k for k in picked if k not in exclusive]
            async with gates.hold(exclusive=exclusive, shared=shared):
                # 进入临界区：无 await，检查+登记原子
                for k in picked:
                    mode = "exclusive" if k in set(exclusive) else "shared"
                    current = holders.get(k, [])
                    if mode == "exclusive" and current:
                        violations.append(f"w{wid}: exclusive {k} with {current}")
                    if mode == "shared" and any(m == "exclusive" for _, m in current):
                        violations.append(f"w{wid}: shared {k} with {current}")
                    holders.setdefault(k, []).append((wid, mode))
                await asyncio.sleep(0)
                # 退出临界区：无 await，移除原子
                for k in picked:
                    mode = "exclusive" if k in set(exclusive) else "shared"
                    holders[k].remove((wid, mode))
                    if not holders[k]:
                        holders.pop(k)

    async def run_all():
        await asyncio.gather(*(worker(i) for i in range(32)))

    await asyncio.wait_for(run_all(), timeout=20)
    assert violations == []
    assert holders == {}
    assert gates.held_keys() == frozenset()
    assert gates.waiting_count() == 0


async def test_module_singleton_exists_and_is_symbol_gate_set():
    assert isinstance(GATES, SymbolGateSet)


async def test_held_keys_reports_all_holders():
    gates = SymbolGateSet()
    k1, k2 = _key(1), _key(2)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with gates.hold(exclusive=[k1], shared=[k2]):
            entered.set()
            await release.wait()

    t = asyncio.create_task(holder())
    await entered.wait()
    assert gates.held_keys() == frozenset({k1, k2})
    assert gates.held_keys_by_current_task() == frozenset()
    m = gates.metrics()
    assert m.holders == 1 and m.held_keys == 2 and m.waiting == 0
    release.set()
    await asyncio.wait_for(t, timeout=5)


async def test_repeated_cancellation_while_waiting_never_leaks_keys():
    """reviewer blocker 1：等待中被反复 cancel，释放路径必须是同步的、不能泄漏键。"""
    gates = SymbolGateSet()
    k = _key(1)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with gates.hold(exclusive=[k]):
            entered.set()
            await release.wait()

    async def waiter():
        async with gates.hold(exclusive=[k]):
            return "held"

    h = asyncio.create_task(holder())
    await entered.wait()
    w = asyncio.create_task(waiter())
    await asyncio.sleep(0.01)
    assert gates.waiting_count() == 1

    w.cancel()
    w.cancel()  # 二次取消：旧实现 await Condition 释放会被打断 → 永久泄漏
    with pytest.raises(asyncio.CancelledError):
        await w
    assert gates.waiting_count() == 0
    assert gates.held_keys() == frozenset({k})   # holder 仍持有，waiter 未泄漏

    release.set()
    await asyncio.wait_for(h, timeout=5)
    assert gates.held_keys() == frozenset()
    # 泄漏的话这里会永远拿不到
    async with gates.hold(exclusive=[k]):
        assert gates.held_keys_by_current_task() == frozenset({k})


async def test_cancel_right_after_grant_still_releases():
    """授予与取消同时发生：已登记的键必须在 finally 里同步释放。"""
    gates = SymbolGateSet()
    k = _key(1)
    entered = asyncio.Event()
    about_to_release = asyncio.Event()

    async def holder():
        async with gates.hold(exclusive=[k]):
            entered.set()
            await about_to_release.wait()

    async def waiter():
        async with gates.hold(exclusive=[k]):
            await asyncio.sleep(0.05)
            return "held"

    h = asyncio.create_task(holder())
    await entered.wait()
    w = asyncio.create_task(waiter())
    await asyncio.sleep(0.01)
    assert gates.waiting_count() == 1

    about_to_release.set()
    await asyncio.sleep(0)      # holder 退出 → 授予 w（w 可能尚未恢复执行）
    w.cancel()
    with pytest.raises(asyncio.CancelledError):
        await w

    assert gates.held_keys() == frozenset()
    async with gates.hold(exclusive=[k]):
        pass
    await asyncio.wait_for(h, timeout=5)
    assert gates.held_keys() == frozenset()


async def test_queued_exclusive_not_starved_by_late_shared():
    """reviewer blocker 4：FIFO + 写者优先，后到的共享请求不得插队。"""
    gates = SymbolGateSet()
    k = _key(1)
    reader_in = asyncio.Event()
    release_reader = asyncio.Event()
    order: list[str] = []

    async def early_shared():
        async with gates.hold(shared=[k]):
            order.append("reader1-in")
            reader_in.set()
            await release_reader.wait()
            order.append("reader1-out")

    async def queued_exclusive():
        async with gates.hold(exclusive=[k]):
            order.append("writer-in")
            await asyncio.sleep(0)
            order.append("writer-out")

    async def late_shared():
        async with gates.hold(shared=[k]):
            order.append("reader2-in")

    t1 = asyncio.create_task(early_shared())
    await reader_in.wait()
    tw = asyncio.create_task(queued_exclusive())
    await asyncio.sleep(0.01)                     # writer 进入队列
    assert gates.waiting_count() == 1

    t2 = asyncio.create_task(late_shared())       # 后到 shared：必须排在 writer 后面
    await asyncio.sleep(0.01)
    assert gates.waiting_count() == 2
    assert "reader2-in" not in order

    release_reader.set()
    await asyncio.wait_for(asyncio.gather(t1, tw, t2), timeout=5)
    assert order.index("writer-in") < order.index("reader2-in")
    assert order.index("writer-out") < order.index("reader2-in")
    assert gates.held_keys() == frozenset()


async def test_fifo_same_mode_requests_keep_arrival_order():
    gates = SymbolGateSet()
    k = _key(2)
    holder_in = asyncio.Event()
    release_holder = asyncio.Event()
    order: list[int] = []

    async def holder():
        async with gates.hold(exclusive=[k]):
            holder_in.set()
            await release_holder.wait()

    def waiter(idx):
        async def _run():
            async with gates.hold(exclusive=[k]):
                order.append(idx)
        return _run

    h = asyncio.create_task(holder())
    await holder_in.wait()
    waiters = []
    for i in range(4):
        waiters.append(asyncio.create_task(waiter(i)()))
        await asyncio.sleep(0.01)                 # 保证入队顺序确定
    assert gates.waiting_count() == 4
    release_holder.set()
    await asyncio.wait_for(asyncio.gather(h, *waiters), timeout=5)
    assert order == [0, 1, 2, 3]
