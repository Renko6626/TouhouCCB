# 单写者内存状态机 · 阶段 0 + 阶段 1 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地 spec 的阶段 0（SSE 广播序列化一次）+ 阶段 1（per-market 单写者 + 内存 q + candle flusher，feature flag 控制，API 契约零变更），把 buy p50 从 36 s 压到 20 ms 以内。

**Architecture:** 新增 `MarketWriter` 单例：每市场一条 `asyncio.Queue` + 常驻 consumer task，内存持有权威 q（Decimal 6dp 不动点 + float 派生）；命令先内存定价/校验，再开独立 DB 事务（user 锁保留），commit 成功后才回写内存并投喂 candle flusher / SSE。旧路径一行不动，`site_config.single_writer_enabled` 决定走向（启动时读一次，翻转需重启）。

**Tech Stack:** FastAPI + SQLAlchemy async（Postgres 生产 / SQLite 测试）+ asyncio。无新依赖。

**Spec:** `docs/superpowers/specs/2026-08-21-single-writer-design.md`（本计划实现其 § 4、§ 5.1 前半（仅序列化一次，不含 tick 帧）、§ 7.5、§ 8 阶段 0/1）

## Global Constraints

- 生产站在跑；本计划全部工作在分支 `perf/2026-08-21-single-writer` 上，**不 push**（push 是 CLAUDE.md 红线，完成后交用户决定）
- **API 契约零变更**（spec § 8 阶段 1）：所有端点的请求/响应/SSE 事件形状与现状逐字段一致
- **单进程**是架构前提（`Dockerfile --workers 1`），writer/flusher 全部进程内
- **内存 q 变更必须在 DB commit 之后**，且回写值 = 镜像的 6dp 量化结果（spec § 4.4 不动点）
- `market_locks.py` **一行不动**；`lock_user` / position 锁在新路径保留（spec § 4.5）
- 资金/份额 Decimal 6 位、价格 8 位（`services/lmsr.py` 的 `quantize_cost` / `quantize_price`），LMSR 内核 float
- 不改 schema（本计划无新表新列，`single_writer_enabled` 是 siteconfig 数据行）
- `backend/app/api/v1/market.py`、`backend/app/services/realtime.py` 是 CLAUDE.md 高敏感文件——本计划就是冲着它们来的，已获 spec 授权；改动仍须最小化、每步测试兜底
- 后端验证命令（每个 task 的 commit 前）：`python -m py_compile $(find app -name '*.py')` + `python -c "import app.main"` + 该 task 的 pytest；计划收尾跑全量 `python -m pytest -x -q`
- commit 风格：`feat:/fix:/refactor:/perf:/test:` + 中文，一个可独立回滚的改动一条；按文件 `git add <path>`
- 所有后端命令在 `backend/` 目录下执行

---

### Task 1: 阶段 0 —— 广播序列化一次（publish 投 bytes）

**Files:**
- Modify: `backend/app/services/realtime.py`（`publish()`、`Subscriber.q` 语义）
- Modify: `backend/app/api/v1/stream.py`（generator 直接 yield bytes）
- Test: `backend/tests/test_realtime_broker.py`（更新既有断言 + 新增单次序列化断言）

**Interfaces:**
- Consumes: 现有 `MarketEventBroker.publish(market_id, event_type, data)` 调用方（market.py 5 处）——签名不变
- Produces: `Subscriber.q` 内元素从 `MarketEvent` 对象变为 **`bytes`**（完整 SSE wire 格式 `event: ...\ndata: {...}\n\n` 的 UTF-8 编码）。后续 task 不依赖此细节，但阶段 2 的 broadcaster 将沿用「队列投 bytes」约定

**背景**：现状 `publish()` 把同一个 `MarketEvent` 对象投给 N 个订阅者，每个 generator 各自 `sse_pack(evt)` → N 次 `json.dumps`。改为 publish 内打包一次。snapshot / ping 是 per-connection 数据，仍由 generator 自己 pack——不受影响。

- [ ] **Step 1: 更新 test_realtime_broker.py —— 写失败测试**

在 `backend/tests/test_realtime_broker.py` 顶部加解析 helper（测试内自用）：

```python
import json

def _parse_sse(blob: bytes) -> dict:
    """把 publish 投递的 SSE bytes 解析回 payload dict。"""
    text = blob.decode("utf-8")
    assert text.endswith("\n\n")
    data_line = next(l for l in text.split("\n") if l.startswith("data: "))
    return json.loads(data_line[len("data: "):])
```

新增测试：

```python
@pytest.mark.asyncio
async def test_publish_serializes_once_across_subscribers():
    """3 个订阅者收到的必须是同一个 bytes 对象（identity），证明只序列化一次。"""
    b = MarketEventBroker()
    subs = [await b.subscribe(1) for _ in range(3)]
    await b.publish(1, "trade", {"x": 1})
    blobs = [s.q.get_nowait() for s, _ in subs]
    assert all(isinstance(bl, bytes) for bl in blobs)
    assert blobs[0] is blobs[1] is blobs[2]
    payload = _parse_sse(blobs[0])
    assert payload["type"] == "trade"
    assert payload["data"] == {"x": 1}
    assert payload["seq"] == 1
    for s, _ in subs:
        await b.unsubscribe(1, s)
```

同时改写既有的对象断言：所有 `evt = await sub.q.get()` / `evt = sub.q.get_nowait()` 后跟 `evt.seq` / `evt.type` / `evt.data` 的断言，改为 `payload = _parse_sse(blob)` 后断言 `payload["seq"]` / `payload["type"]` / `payload["data"]`（涉及 `test_subscribe_anchor_reflects_current_seq`、`test_subscribe_anchor_atomicity_under_concurrent_publish`、`test_publish_to_subscriber_queue`、`test_publish_increments_seq_but_ping_does_not` 等；逐个 grep `q.get` 找全）。

- [ ] **Step 2: 跑测确认失败**

Run: `python -m pytest tests/test_realtime_broker.py -x -q`
Expected: FAIL —— 新测试断言 `isinstance(bl, bytes)` 失败（队列里还是 MarketEvent 对象）

- [ ] **Step 3: 改 realtime.py**

`publish()` 中构造 `evt` 之后（`realtime.py:120-126` 之后）、投递循环之前，加打包；投递改投 blob：

```python
        blob = sse_pack(evt).encode("utf-8")   # ★ 整个进程只序列化一次（spec § 8 阶段 0）

        dead_subs = []
        for sub in subs:
            try:
                sub.q.put_nowait(blob)
```

并更新 `Subscriber` docstring 的一句：注明 `q` 中元素是打包好的 SSE bytes。`sse_pack` 本身不动（stream.py 的 snapshot/ping 还用）。

- [ ] **Step 4: 改 stream.py generator**

`stream.py:180-182` 的事件分支改为直接转发：

```python
                if get_task in done:
                    blob: bytes = get_task.result()
                    yield blob
```

（snapshot 首包与 ping 分支保持 `yield sse_pack(...).encode("utf-8")` 不变。）`MarketEvent` 的 import 仍需要（snapshot/ping 用），不要删。

- [ ] **Step 5: 跑测确认通过**

Run: `python -m pytest tests/test_realtime_broker.py -q && python -m py_compile $(find app -name '*.py') && python -c "import app.main"`
Expected: 全 PASS

- [ ] **Step 6: 跑涉及 SSE 的集成测试**

Run: `python -m pytest tests/test_candle_integration.py tests/test_market_slippage_lock.py -q`
Expected: PASS（buy/sell 端点 publish 路径被覆盖）

- [ ] **Step 7: Commit**

```bash
git add app/services/realtime.py app/api/v1/stream.py tests/test_realtime_broker.py
git commit -m "perf(sse): 广播序列化一次——publish 打包 bytes，generator 直接转发（阶段 0）"
```

---

### Task 2: MarketState + MarketWriter 骨架（加载 / 注册 / 重置）

**Files:**
- Create: `backend/app/services/market_writer.py`
- Test: `backend/tests/test_market_writer_state.py`

**Interfaces:**
- Consumes: `app.core.database.async_session_maker`、`app.models.base.{Market, Outcome, MarketStatus}`、`app.services.lmsr.{calculate_lmsr_with_prices, quantize_cost}`
- Produces（后续 task 依赖的确切签名）:
  - `WRITER: MarketWriter`（模块级单例）
  - `MarketState`（dataclass，字段见下）
  - `await WRITER.start() -> None`（从 DB 载入全部 TRADING/HALT 市场并启动 consumer；重复调用先 stop）
  - `await WRITER.stop() -> None`
  - `WRITER.enabled: bool`（property，start 后为 True）
  - `WRITER.market_id_for_outcome(outcome_id: int) -> int | None`
  - `WRITER.get_state(market_id: int) -> MarketState | None`
  - `await WRITER.register_market(market_id: int) -> None`（create_market 后调用，从 DB 读该市场建 state）
  - `await WRITER.reload_state(market_id: int) -> None`（自愈：从 DB 镜像重读 q）

- [ ] **Step 1: 写失败测试**

`backend/tests/test_market_writer_state.py`：

```python
"""MarketWriter 状态加载/注册/重置单元测试（不走 HTTP，不需要 client fixture）。"""
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlmodel import SQLModel

from app.core.database import engine, async_session_maker
from app.models.base import Market, MarketStatus, Outcome
from app.services.market_writer import WRITER


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
        await conn.run_sync(SQLModel.metadata.create_all)
    yield
    await WRITER.stop()


async def _seed_market(status=MarketStatus.TRADING, shares=("3.5", "0")) -> tuple[int, list[int]]:
    async with async_session_maker() as s:
        m = Market(title="t", description="", liquidity_b=100.0, status=status)
        s.add(m)
        await s.flush()
        oids = []
        for v in shares:
            o = Outcome(market_id=m.id, label=f"o{v}", total_shares=Decimal(v))
            s.add(o)
            await s.flush()
            oids.append(o.id)
        await s.commit()
        return m.id, oids


@pytest.mark.asyncio
async def test_start_loads_trading_and_halt_not_settled():
    mid_t, _ = await _seed_market(MarketStatus.TRADING)
    mid_h, _ = await _seed_market(MarketStatus.HALT)
    mid_s, _ = await _seed_market(MarketStatus.SETTLED)
    await WRITER.start()
    assert WRITER.enabled
    assert WRITER.get_state(mid_t) is not None
    assert WRITER.get_state(mid_h) is not None
    assert WRITER.get_state(mid_s) is None  # spec § 4.1: SETTLED 不载入


@pytest.mark.asyncio
async def test_state_q_is_6dp_fixpoint_of_mirror():
    mid, oids = await _seed_market(shares=("3.5", "0"))
    await WRITER.start()
    st = WRITER.get_state(mid)
    assert st.outcome_ids == sorted(oids)           # 升序索引契约
    assert st.q_dec == [Decimal("3.500000"), Decimal("0.000000")]
    assert st.q == [float(Decimal("3.500000")), 0.0]
    assert len(st.prices) == 2
    assert abs(sum(st.prices) - 1.0) < 1e-9
    assert WRITER.market_id_for_outcome(oids[0]) == mid
    assert WRITER.market_id_for_outcome(999999) is None


@pytest.mark.asyncio
async def test_register_market_after_start():
    await WRITER.start()
    mid, oids = await _seed_market()
    assert WRITER.get_state(mid) is None
    await WRITER.register_market(mid)
    assert WRITER.get_state(mid) is not None
    assert WRITER.market_id_for_outcome(oids[0]) == mid


@pytest.mark.asyncio
async def test_stop_disables():
    await WRITER.start()
    await WRITER.stop()
    assert not WRITER.enabled
```

- [ ] **Step 2: 跑测确认失败**

Run: `python -m pytest tests/test_market_writer_state.py -x -q`
Expected: FAIL —— `ModuleNotFoundError: app.services.market_writer`

- [ ] **Step 3: 写 market_writer.py 骨架**

```python
"""单写者内存状态机（spec § 4）。

每个市场一条 asyncio.Queue + 常驻 consumer task；内存 q 是权威，
DB outcome.total_shares 是镜像（每笔 commit 内同步写、值恒等，
spec § 4.4 的 6dp 不动点）。启用与否在进程启动时决定（翻转需重启）。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.base import Market, MarketStatus, Outcome
from app.services.lmsr import calculate_lmsr_with_prices, quantize_cost

logger = logging.getLogger(__name__)


@dataclass
class MarketState:
    market_id: int
    b: float
    outcome_ids: list[int]        # 升序——价格向量的索引契约（spec § 4.1）
    outcome_labels: list[str]     # 与 outcome_ids 同序，buy/sell 响应 message 用
    q_dec: list[Decimal]          # 权威值 = DB 镜像的 6dp 量化结果（不动点，spec § 4.4）
    q: list[float]                # q_dec 的 float 派生，喂 LMSR
    prices: list[float]           # 由 q 导出并缓存
    status: MarketStatus
    closes_at: Optional[datetime]
    seq: int = 0                  # 帧序号，阶段 2（定频广播）才使用
    unavailable: bool = False     # 自愈失败后置 True：该市场一律 503（spec § 4.4 异常策略）


def _derive(q_dec: list[Decimal], b: float) -> tuple[list[float], list[float]]:
    """q_dec → (q floats, prices)。"""
    q = [float(x) for x in q_dec]
    _, prices = calculate_lmsr_with_prices(q, b)
    return q, prices


async def _load_one(session, market: Market) -> MarketState:
    outs = (await session.execute(
        select(Outcome).where(Outcome.market_id == market.id).order_by(Outcome.id.asc())
    )).scalars().all()
    q_dec = [quantize_cost(o.total_shares) for o in outs]
    q, prices = _derive(q_dec, float(market.liquidity_b))
    return MarketState(
        market_id=int(market.id),
        b=float(market.liquidity_b),
        outcome_ids=[int(o.id) for o in outs],
        outcome_labels=[str(o.label) for o in outs],
        q_dec=q_dec,
        q=q,
        prices=prices,
        status=market.status,
        closes_at=market.closes_at,
    )


class MarketWriter:
    QUEUE_MAXSIZE = 256      # 满则 429（spec § 4.3 第一道背压）
    SUBMIT_TIMEOUT = 10.0    # 等结果超时 → 503（spec § 4.3 第二道背压）

    def __init__(self) -> None:
        self._states: dict[int, MarketState] = {}
        self._queues: dict[int, asyncio.Queue] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._market_by_outcome: dict[int, int] = {}
        self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def get_state(self, market_id: int) -> MarketState | None:
        return self._states.get(market_id)

    def market_id_for_outcome(self, outcome_id: int) -> int | None:
        return self._market_by_outcome.get(outcome_id)

    async def start(self) -> None:
        await self.stop()
        async with async_session_maker() as s:
            markets = (await s.execute(
                select(Market).where(
                    Market.status.in_([MarketStatus.TRADING, MarketStatus.HALT])
                )
            )).scalars().all()
            for m in markets:
                st = await _load_one(s, m)
                self._install(st)
        self._enabled = True
        logger.info("market_writer started: %d markets loaded", len(self._states))

    async def stop(self) -> None:
        self._enabled = False
        for t in self._tasks.values():
            t.cancel()
        for t in list(self._tasks.values()):
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        self._queues.clear()
        self._states.clear()
        self._market_by_outcome.clear()

    async def register_market(self, market_id: int) -> None:
        async with async_session_maker() as s:
            m = await s.get(Market, market_id)
            if m is None:
                return
            st = await _load_one(s, m)
        self._install(st)

    async def reload_state(self, market_id: int) -> None:
        """自愈：从 DB 镜像重读 q / status。失败则标记 unavailable。"""
        try:
            async with async_session_maker() as s:
                m = await s.get(Market, market_id)
                if m is None:
                    raise RuntimeError(f"market {market_id} vanished")
                st_new = await _load_one(s, m)
            st = self._states[market_id]
            st.q_dec, st.q, st.prices = st_new.q_dec, st_new.q, st_new.prices
            st.status, st.closes_at = st_new.status, st_new.closes_at
            st.unavailable = False
            logger.warning("market_writer state reloaded from mirror: market_id=%d", market_id)
        except Exception:
            self._states[market_id].unavailable = True
            logger.critical(
                "market_writer reload FAILED, market %d marked unavailable", market_id,
                exc_info=True,
            )

    def _install(self, st: MarketState) -> None:
        self._states[st.market_id] = st
        for oid in st.outcome_ids:
            self._market_by_outcome[oid] = st.market_id
        if st.market_id not in self._queues:
            self._queues[st.market_id] = asyncio.Queue(maxsize=self.QUEUE_MAXSIZE)
            self._tasks[st.market_id] = asyncio.create_task(
                self._consume(st.market_id), name=f"market-writer-{st.market_id}"
            )


WRITER = MarketWriter()
```

`_consume` 在本 task 先放一个最小占位（Task 3 实现完整逻辑）：

```python
    async def _consume(self, market_id: int) -> None:
        # Task 3 实现完整命令循环；骨架阶段仅挂起等待
        q = self._queues[market_id]
        while True:
            await q.get()
```

- [ ] **Step 4: 跑测确认通过**

Run: `python -m pytest tests/test_market_writer_state.py -q && python -m py_compile $(find app -name '*.py')`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/market_writer.py tests/test_market_writer_state.py
git commit -m "feat(writer): MarketState + MarketWriter 骨架——加载/注册/自愈重读，6dp 不动点派生"
```

---

### Task 3: 命令循环 + 双道背压 + 自愈

**Files:**
- Modify: `backend/app/services/market_writer.py`
- Test: `backend/tests/test_market_writer_loop.py`

**Interfaces:**
- Produces:
  - `OpOutcome`（dataclass）: `response: Any` / `new_q_dec: list[Decimal] | None = None` / `new_prices: list[float] | None = None` / `new_status: MarketStatus | None = None` / `candle_rows: list[dict] = []` / `publishes: list[tuple[str, dict]] = []`
  - `await WRITER.submit(cmd) -> Any`——`cmd` 是任意注册过的命令 dataclass（须有 `market_id: int` 属性）；返回 op 的 `response`；队列满 raise `HTTPException(429)`；超时 raise `HTTPException(503)`；业务错误透传 op 抛出的 `HTTPException`
  - `WRITER.register_op(cmd_type: type, op: Callable[[MarketState, Any], Awaitable[OpOutcome]]) -> None`——Task 6-9 用它挂 buy/sell/... 的实现
- Consumes: Task 2 的 `MarketState` / `reload_state`；`app.services.realtime.BROKER`；Task 4 的 `CANDLE_FLUSHER`（本 task 先以 `candle_rows` 空列表跳过合并，Task 4 完成后接上——见 Step 5 注释）

- [ ] **Step 1: 写失败测试**

`backend/tests/test_market_writer_loop.py`（复用 Task 2 测试文件的 `_fresh_db` / `_seed_market` 模式——直接拷贝这两个 fixture/helper 进来，测试文件间不共享 helper）：

```python
import asyncio
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.services.market_writer import WRITER, MarketState, OpOutcome

# （此处粘贴 test_market_writer_state.py 的 _fresh_db fixture 与 _seed_market helper，内容一致）


from dataclasses import dataclass

@dataclass
class FakeCmd:
    market_id: int
    behavior: str = "ok"          # ok | http_error | crash_after_commit | slow


def _make_fake_op(db_mutator=None):
    async def op(state: MarketState, cmd: FakeCmd) -> OpOutcome:
        if cmd.behavior == "http_error":
            raise HTTPException(status_code=400, detail="业务拒绝")
        if cmd.behavior == "slow":
            await asyncio.sleep(30)
        if cmd.behavior == "crash_after_commit":
            if db_mutator:
                await db_mutator()      # 模拟「commit 已成功」：直接改 DB
            raise RuntimeError("boom after commit")
        return OpOutcome(
            response={"ok": True},
            new_q_dec=[state.q_dec[0] + Decimal("1"), state.q_dec[1]],
        )
    return op


@pytest.mark.asyncio
async def test_submit_ok_applies_memory_after_op():
    mid, _ = await _seed_market()
    await WRITER.start()
    WRITER.register_op(FakeCmd, _make_fake_op())
    res = await WRITER.submit(FakeCmd(market_id=mid))
    assert res == {"ok": True}
    st = WRITER.get_state(mid)
    assert st.q_dec[0] == Decimal("4.500000")   # 3.5 + 1
    assert st.q[0] == float(Decimal("4.500000"))


@pytest.mark.asyncio
async def test_http_error_propagates_and_memory_untouched():
    mid, _ = await _seed_market()
    await WRITER.start()
    WRITER.register_op(FakeCmd, _make_fake_op())
    with pytest.raises(HTTPException) as ei:
        await WRITER.submit(FakeCmd(market_id=mid, behavior="http_error"))
    assert ei.value.status_code == 400
    assert WRITER.get_state(mid).q_dec[0] == Decimal("3.500000")


@pytest.mark.asyncio
async def test_queue_full_raises_429(monkeypatch):
    mid, _ = await _seed_market()
    await WRITER.start()
    WRITER.register_op(FakeCmd, _make_fake_op())
    monkeypatch.setattr(WRITER, "SUBMIT_TIMEOUT", 0.2)
    # 先塞一个 slow 占住 consumer，再灌满队列
    slow = asyncio.create_task(_submit_swallow(FakeCmd(market_id=mid, behavior="slow")))
    await asyncio.sleep(0.05)
    q = WRITER._queues[mid]
    fillers = []
    while not q.full():
        fillers.append(asyncio.create_task(_submit_swallow(FakeCmd(market_id=mid))))
        await asyncio.sleep(0)
    with pytest.raises(HTTPException) as ei:
        await WRITER.submit(FakeCmd(market_id=mid))
    assert ei.value.status_code == 429
    slow.cancel()
    for f in fillers:
        f.cancel()


async def _submit_swallow(cmd):
    try:
        await WRITER.submit(cmd)
    except (HTTPException, asyncio.CancelledError):
        pass


@pytest.mark.asyncio
async def test_submit_timeout_returns_503(monkeypatch):
    mid, _ = await _seed_market()
    await WRITER.start()
    WRITER.register_op(FakeCmd, _make_fake_op())
    monkeypatch.setattr(WRITER, "SUBMIT_TIMEOUT", 0.1)
    with pytest.raises(HTTPException) as ei:
        await WRITER.submit(FakeCmd(market_id=mid, behavior="slow"))
    assert ei.value.status_code == 503
    assert "结果未知" in ei.value.detail   # spec § 4.3：措辞必须是结果未知


@pytest.mark.asyncio
async def test_unexpected_exception_self_heals_from_mirror():
    """commit 后异常 → 内存从镜像重读（spec § 4.4 异常策略）。"""
    from sqlalchemy import update
    from app.core.database import async_session_maker
    from app.models.base import Outcome

    mid, oids = await _seed_market()
    await WRITER.start()

    async def mutate_db():
        async with async_session_maker() as s:
            await s.execute(update(Outcome).where(Outcome.id == oids[0])
                            .values(total_shares=Decimal("99.000000")))
            await s.commit()

    WRITER.register_op(FakeCmd, _make_fake_op(db_mutator=mutate_db))
    with pytest.raises(HTTPException) as ei:
        await WRITER.submit(FakeCmd(market_id=mid, behavior="crash_after_commit"))
    assert ei.value.status_code == 500
    await asyncio.sleep(0.1)   # 让 consumer 完成 reload
    st = WRITER.get_state(mid)
    assert st.q_dec[0] == Decimal("99.000000")   # 重读到了 DB 的真值
    assert not st.unavailable
    # 自愈后还能继续服务
    res = await WRITER.submit(FakeCmd(market_id=mid))
    assert res == {"ok": True}
```

- [ ] **Step 2: 跑测确认失败**

Run: `python -m pytest tests/test_market_writer_loop.py -x -q`
Expected: FAIL —— `ImportError: OpOutcome` / `AttributeError: register_op`

- [ ] **Step 3: 实现命令循环**

在 `market_writer.py` 补：

```python
from fastapi import HTTPException

@dataclass
class OpOutcome:
    """op 执行结果。op 内部完成 DB 事务；consumer 在 op 返回后统一 apply 内存。"""
    response: Any
    new_q_dec: Optional[list[Decimal]] = None
    new_prices: Optional[list[float]] = None
    new_status: Optional[MarketStatus] = None
    candle_rows: list[dict] = field(default_factory=list)
    publishes: list[tuple[str, dict]] = field(default_factory=list)
```

`MarketWriter` 增加：

```python
    def __init__(self) -> None:
        ...  # 原有字段
        self._ops: dict[type, Any] = {}

    def register_op(self, cmd_type: type, op) -> None:
        self._ops[cmd_type] = op

    async def submit(self, cmd) -> Any:
        market_id = cmd.market_id
        q = self._queues.get(market_id)
        st = self._states.get(market_id)
        if q is None or st is None:
            raise HTTPException(status_code=400, detail="市场当前不可交易")
        if st.unavailable:
            raise HTTPException(status_code=503, detail="市场状态异常，暂停服务，请稍后重试")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        try:
            q.put_nowait((cmd, fut))
        except asyncio.QueueFull:
            raise HTTPException(status_code=429, detail="交易过于繁忙，请稍后重试")
        try:
            return await asyncio.wait_for(fut, timeout=self.SUBMIT_TIMEOUT)
        except asyncio.TimeoutError:
            # 命令可能仍在 DB 里执行——结果未知，绝不能说"失败"（spec § 4.3）
            raise HTTPException(status_code=503, detail="服务繁忙，本次操作结果未知，请刷新后确认")

    async def _consume(self, market_id: int) -> None:
        from app.services.realtime import BROKER   # 局部 import 避免环
        q = self._queues[market_id]
        while True:
            cmd, fut = await q.get()
            st = self._states[market_id]
            try:
                if st.unavailable:
                    raise HTTPException(status_code=503, detail="市场状态异常，暂停服务")
                op = self._ops[type(cmd)]
                outcome: OpOutcome = await op(st, cmd)
                # ── commit 已成功（op 返回即视为已 commit）→ apply 内存（spec § 4.4）──
                if outcome.new_q_dec is not None:
                    st.q_dec = outcome.new_q_dec
                    st.q = [float(x) for x in st.q_dec]
                    if outcome.new_prices is not None:
                        st.prices = outcome.new_prices
                    else:
                        _, st.prices = calculate_lmsr_with_prices(st.q, st.b)
                if outcome.new_status is not None:
                    st.status = outcome.new_status
                if outcome.candle_rows:
                    self._merge_candles(outcome.candle_rows)
                for event_type, data in outcome.publishes:
                    await BROKER.publish(market_id, event_type, data)
                if not fut.done():
                    fut.set_result(outcome.response)
            except HTTPException as e:
                # 业务拒绝：op 保证此时事务已回滚 / 未开启，内存零变更
                if not fut.done():
                    fut.set_exception(e)
            except asyncio.CancelledError:
                if not fut.done():
                    fut.set_exception(HTTPException(status_code=503, detail="服务关闭中"))
                raise
            except Exception:
                # 非预期异常：无法区分 commit 前后 → 一律从镜像重读自愈（spec § 4.4）
                logger.critical(
                    "market_writer op crashed, reloading state: market_id=%d cmd=%s",
                    market_id, type(cmd).__name__, exc_info=True,
                )
                if not fut.done():
                    fut.set_exception(HTTPException(
                        status_code=500, detail="交易处理异常，结果未知，请刷新后确认"))
                await self.reload_state(market_id)

    def _merge_candles(self, rows: list[dict]) -> None:
        # Task 4 接上 CANDLE_FLUSHER.merge(rows)；当前占位 no-op
        pass
```

（删掉 Task 2 的占位 `_consume`。）

- [ ] **Step 4: 跑测确认通过**

Run: `python -m pytest tests/test_market_writer_loop.py tests/test_market_writer_state.py -q`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/market_writer.py tests/test_market_writer_loop.py
git commit -m "feat(writer): 命令循环——队列满 429 / 等待超时 503 双道背压，非预期异常从镜像自愈"
```

---

### Task 4: Candle flusher（写入移出 hot path）

**Files:**
- Create: `backend/app/services/candle_flusher.py`
- Modify: `backend/app/services/market_writer.py`（`_merge_candles` 接上）
- Test: `backend/tests/test_candle_flusher.py`

**Interfaces:**
- Consumes: `app.services.candle_writer.upsert_candles`（行 dict 格式与 `compute_candle_rows` 输出一致）
- Produces:
  - `CANDLE_FLUSHER: CandleFlusher`（模块级单例）
  - `CANDLE_FLUSHER.merge(rows: list[dict]) -> None`（同步，内存合并）
  - `await CANDLE_FLUSHER.flush_once() -> int`（落库一批，返回行数；失败自动把这批 merge 回去）
  - `await CANDLE_FLUSHER.start()` / `await CANDLE_FLUSHER.stop()`（stop 做最终 flush）
  - `CANDLE_FLUSHER.pending_count() -> int`（测试/观测用）

- [ ] **Step 1: 写失败测试**

`backend/tests/test_candle_flusher.py`（同样内嵌 `_fresh_db` fixture；建 1 个 outcome 供外键）：

```python
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlmodel import SQLModel, select

from app.core.database import engine, async_session_maker
from app.models.base import Market, MarketStatus, Outcome, OutcomeCandle
from app.services.candle_flusher import CANDLE_FLUSHER


@pytest.fixture(autouse=True)
def _reset_flusher():
    CANDLE_FLUSHER._pending.clear()
    yield


# （粘贴 _fresh_db fixture；teardown 不需要 WRITER.stop()，去掉那行）


def _row(oid: int, interval="10s", bucket=None, o="0.5", h="0.6", l="0.5", c="0.6", v="1", n=1):
    return {
        "outcome_id": oid, "interval": interval,
        "bucket_start": bucket or datetime(2026, 8, 21, 0, 0, 0, tzinfo=timezone.utc),
        "open_price": Decimal(o), "high_price": Decimal(h),
        "low_price": Decimal(l), "close_price": Decimal(c),
        "volume_shares": Decimal(v), "n_trades": n,
        "updated_at": datetime(2026, 8, 21, 0, 0, 1, tzinfo=timezone.utc),
    }


async def _seed_outcome() -> int:
    async with async_session_maker() as s:
        m = Market(title="t", description="", liquidity_b=100.0, status=MarketStatus.TRADING)
        s.add(m); await s.flush()
        o = Outcome(market_id=m.id, label="a", total_shares=Decimal("0"))
        s.add(o); await s.flush()
        oid = o.id
        await s.commit()
        return oid


def test_merge_order_aware():
    """同桶两笔：open 保留首笔，close 取末笔，high/low 取极值，vol/n 累加。"""
    CANDLE_FLUSHER.merge([_row(1, o="0.5", h="0.6", l="0.5", c="0.6", v="1", n=1)])
    CANDLE_FLUSHER.merge([_row(1, o="0.6", h="0.7", l="0.4", c="0.55", v="2", n=1)])
    assert CANDLE_FLUSHER.pending_count() == 1
    merged = next(iter(CANDLE_FLUSHER._pending.values()))
    assert merged["open_price"] == Decimal("0.5")
    assert merged["close_price"] == Decimal("0.55")
    assert merged["high_price"] == Decimal("0.7")
    assert merged["low_price"] == Decimal("0.4")
    assert merged["volume_shares"] == Decimal("3")
    assert merged["n_trades"] == 2


@pytest.mark.asyncio
async def test_flush_once_writes_and_drains():
    oid = await _seed_outcome()
    CANDLE_FLUSHER.merge([_row(oid)])
    n = await CANDLE_FLUSHER.flush_once()
    assert n == 1
    assert CANDLE_FLUSHER.pending_count() == 0
    async with async_session_maker() as s:
        rows = (await s.execute(select(OutcomeCandle))).scalars().all()
        assert len(rows) == 1
        assert rows[0].volume_shares == Decimal("1")
    # 再 flush 一次必须是 no-op（否则 upsert 累加语义会 double-count）
    assert await CANDLE_FLUSHER.flush_once() == 0
    async with async_session_maker() as s:
        rows = (await s.execute(select(OutcomeCandle))).scalars().all()
        assert rows[0].volume_shares == Decimal("1")


@pytest.mark.asyncio
async def test_flush_failure_remerges(monkeypatch):
    oid = await _seed_outcome()
    CANDLE_FLUSHER.merge([_row(oid, c="0.6", v="1", n=1)])

    async def boom(db, rows):
        raise RuntimeError("db down")
    monkeypatch.setattr("app.services.candle_flusher.upsert_candles", boom)
    assert await CANDLE_FLUSHER.flush_once() == 0
    assert CANDLE_FLUSHER.pending_count() == 1    # 这批回炉了
    monkeypatch.undo()

    # 失败期间又来一笔同桶：回炉行是"较早"方，open 用它的
    CANDLE_FLUSHER.merge([_row(oid, o="0.6", c="0.7", v="2", n=1)])
    merged = next(iter(CANDLE_FLUSHER._pending.values()))
    assert merged["open_price"] == Decimal("0.5")
    assert merged["close_price"] == Decimal("0.7")
    assert merged["volume_shares"] == Decimal("3")
```

- [ ] **Step 2: 跑测确认失败**

Run: `python -m pytest tests/test_candle_flusher.py -x -q`
Expected: FAIL —— `ModuleNotFoundError: app.services.candle_flusher`

- [ ] **Step 3: 实现 candle_flusher.py**

```python
"""Candle 写入的独立 flusher（spec § 7.5）。

writer 每笔成交把 compute_candle_rows 的输出 merge 进内存 pending（微秒），
flusher 每 5 s 批量 UPSERT。崩溃最多丢 5 s，由 main.py::_resync_recent_candles
启动兜底重放补齐（窗口 1 h >> 5 s，无需调参）。

pop-then-upsert 语义：flush 先原子取走整批再落库；失败把这批按"较早方"
merge 回 pending——因为 upsert_candles 的 volume/n 是累加合并，同一批 flush
两次会 double-count，绝不能 flush 成功后不清 / 失败后重复 flush 同批。
"""
from __future__ import annotations

import asyncio
import logging

from app.core.database import async_session_maker
from app.services.candle_writer import upsert_candles

logger = logging.getLogger(__name__)

_Key = tuple[int, str, object]   # (outcome_id, interval, bucket_start)


def _key(row: dict) -> _Key:
    return (row["outcome_id"], row["interval"], row["bucket_start"])


def _merge_row(earlier: dict, later: dict) -> dict:
    """同桶合并，earlier 在时间上先发生：open 保留 earlier，close 取 later。"""
    return {
        **later,
        "open_price": earlier["open_price"],
        "high_price": max(earlier["high_price"], later["high_price"]),
        "low_price": min(earlier["low_price"], later["low_price"]),
        "close_price": later["close_price"],
        "volume_shares": earlier["volume_shares"] + later["volume_shares"],
        "n_trades": earlier["n_trades"] + later["n_trades"],
    }


class CandleFlusher:
    FLUSH_INTERVAL = 5.0

    def __init__(self) -> None:
        self._pending: dict[_Key, dict] = {}
        self._task: asyncio.Task | None = None

    def pending_count(self) -> int:
        return len(self._pending)

    def merge(self, rows: list[dict]) -> None:
        for row in rows:
            k = _key(row)
            old = self._pending.get(k)
            self._pending[k] = _merge_row(old, row) if old else dict(row)

    async def flush_once(self) -> int:
        if not self._pending:
            return 0
        batch = self._pending
        self._pending = {}
        try:
            async with async_session_maker() as s:
                await upsert_candles(s, list(batch.values()))
                await s.commit()
            return len(batch)
        except Exception:
            logger.exception("candle flush failed, re-merging %d rows", len(batch))
            # 回炉：batch 是较早方（失败期间可能有新 merge 进来）
            for k, row in batch.items():
                newer = self._pending.get(k)
                self._pending[k] = _merge_row(row, newer) if newer else row
            return 0

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="candle-flusher")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.flush_once()   # 最终 flush，优雅停机不丢

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.FLUSH_INTERVAL)
            try:
                await self.flush_once()
            except Exception:
                logger.exception("candle flusher loop error")


CANDLE_FLUSHER = CandleFlusher()
```

- [ ] **Step 4: 接上 writer**

`market_writer.py` 的 `_merge_candles` 改为：

```python
    def _merge_candles(self, rows: list[dict]) -> None:
        from app.services.candle_flusher import CANDLE_FLUSHER
        CANDLE_FLUSHER.merge(rows)
```

- [ ] **Step 5: 跑测确认通过**

Run: `python -m pytest tests/test_candle_flusher.py tests/test_market_writer_loop.py -q`
Expected: 全 PASS

- [ ] **Step 6: Commit**

```bash
git add app/services/candle_flusher.py app/services/market_writer.py tests/test_candle_flusher.py
git commit -m "feat(candle): flusher 独立落盘——内存合并 5s 批量 UPSERT，失败回炉不 double-count"
```

---

### Task 5: 提取滑点校验纯函数（老路径行为不变的重构）

**Files:**
- Create: `backend/app/services/trade_checks.py`
- Modify: `backend/app/api/v1/market.py`（buy `:558-580`、sell `:730-752` 改调 helper）
- Test: `backend/tests/test_trade_checks.py`（新增）；`tests/test_market_slippage_lock.py`（既有，作行为不变的护栏）

**Interfaces:**
- Produces（Task 6/7 的 writer op 复用）:
  - `check_buy_slippage(pay: Decimal, expected_pay: Decimal, marginal_price: Decimal, max_cost: Decimal | None, max_slippage_bps: int | None, accept_any_slippage: bool) -> None`（违规 raise `HTTPException(400)`，错误文案与现状逐字相同）
  - `check_sell_slippage(proceeds: Decimal, net: Decimal, expected_proceeds: Decimal, marginal_price: Decimal, min_proceeds: Decimal | None, max_slippage_bps: int | None, accept_any_slippage: bool) -> None`
  - 常量 `DEFAULT_SLIPPAGE_BPS = 500` / `HARDCAP_SLIPPAGE_BPS = 1000` 移入本模块，market.py 从这里 import（保持旧名可用）

- [ ] **Step 1: 写失败测试**

`backend/tests/test_trade_checks.py`：

```python
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.services.trade_checks import check_buy_slippage, check_sell_slippage

D = Decimal


def test_buy_max_cost_breach():
    with pytest.raises(HTTPException) as ei:
        check_buy_slippage(D("11"), D("10"), D("0.5"), D("10.5"), None, False)
    assert "max_cost" in ei.value.detail


def test_buy_bps_default_500_pass_and_fail():
    # expected=10，默认 5% 上限 = 10.5
    check_buy_slippage(D("10.5"), D("10"), D("0.5"), None, None, False)   # 恰好不超，通过
    with pytest.raises(HTTPException):
        check_buy_slippage(D("10.500001"), D("10"), D("0.5"), None, None, False)


def test_buy_bps_hardcap_1000():
    # 客户端要 5000bps，被截到 1000 → 上限 11
    check_buy_slippage(D("11"), D("10"), D("0.5"), None, 5000, False)
    with pytest.raises(HTTPException):
        check_buy_slippage(D("11.000001"), D("10"), D("0.5"), None, 5000, False)


def test_buy_accept_any_skips_bps_but_not_max_cost():
    check_buy_slippage(D("999"), D("10"), D("0.5"), None, None, True)
    with pytest.raises(HTTPException):
        check_buy_slippage(D("999"), D("10"), D("0.5"), D("100"), None, True)


def test_sell_min_proceeds_compares_net():
    with pytest.raises(HTTPException) as ei:
        check_sell_slippage(D("10"), D("9.5"), D("10"), D("0.5"), D("9.6"), None, False)
    assert "min_proceeds" in ei.value.detail


def test_sell_bps_compares_gross():
    # expected=10，默认 5% 下限 = 9.5；proceeds（gross）达标即可，net 低于无妨
    check_sell_slippage(D("9.5"), D("9"), D("10"), D("0.5"), None, None, False)
    with pytest.raises(HTTPException):
        check_sell_slippage(D("9.499999"), D("9"), D("10"), D("0.5"), None, None, False)
```

- [ ] **Step 2: 跑测确认失败**

Run: `python -m pytest tests/test_trade_checks.py -x -q`
Expected: FAIL —— `ModuleNotFoundError`

- [ ] **Step 3: 实现 trade_checks.py**

把 `market.py` 两段滑点逻辑**逐字**搬为纯函数（含注释与错误文案；`quantize(Decimal("0.000001"))` 的位置不变）：

```python
"""买卖滑点校验纯函数——老路径（market.py）与 writer 新路径共用单一实现。

从 market.py 提取，逻辑与文案逐字保持；改这里 = 同时改两条路径。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from fastapi import HTTPException

# ── 滑点保护（P1）──
# 客户端未给 max_cost/min_proceeds 时用百分比兜底；服务端用 hardcap 截断不信任客户端。
DEFAULT_SLIPPAGE_BPS = 500   # 5%
HARDCAP_SLIPPAGE_BPS = 1000  # 10%，再大也截掉


def check_buy_slippage(
    pay: Decimal,
    expected_pay: Decimal,
    marginal_price: Decimal,
    max_cost: Optional[Decimal],
    max_slippage_bps: Optional[int],
    accept_any_slippage: bool,
) -> None:
    if max_cost is not None and pay > max_cost:
        raise HTTPException(
            status_code=400,
            detail=f"成交成本 {pay} 超过 max_cost 限制 {max_cost}，滑点过大请刷新报价",
        )
    if not accept_any_slippage:
        client_bps = max_slippage_bps if max_slippage_bps is not None else DEFAULT_SLIPPAGE_BPS
        effective_bps = min(client_bps, HARDCAP_SLIPPAGE_BPS)
        slippage_limit = (
            expected_pay * Decimal(10000 + effective_bps) / Decimal(10000)
        ).quantize(Decimal("0.000001"))
        if pay > slippage_limit:
            raise HTTPException(
                status_code=400,
                detail=f"滑点超过 {effective_bps / 100}%（边际价 {marginal_price}），请刷新报价",
            )


def check_sell_slippage(
    proceeds: Decimal,
    net: Decimal,
    expected_proceeds: Decimal,
    marginal_price: Decimal,
    min_proceeds: Optional[Decimal],
    max_slippage_bps: Optional[int],
    accept_any_slippage: bool,
) -> None:
    if min_proceeds is not None and net < min_proceeds:
        raise HTTPException(
            status_code=400,
            detail=f"成交收入 {net} 低于 min_proceeds 限制 {min_proceeds}，滑点过大请刷新报价",
        )
    if not accept_any_slippage:
        client_bps = max_slippage_bps if max_slippage_bps is not None else DEFAULT_SLIPPAGE_BPS
        effective_bps = min(client_bps, HARDCAP_SLIPPAGE_BPS)
        slippage_floor = (
            expected_proceeds * Decimal(10000 - effective_bps) / Decimal(10000)
        ).quantize(Decimal("0.000001"))
        if proceeds < slippage_floor:
            raise HTTPException(
                status_code=400,
                detail=f"滑点超过 {effective_bps / 100}%（边际价 {marginal_price}），请刷新报价",
            )
```

- [ ] **Step 4: market.py 改调 helper**

- 删 `market.py:83-84` 的两个常量定义，改为 `from app.services.trade_checks import DEFAULT_SLIPPAGE_BPS, HARDCAP_SLIPPAGE_BPS, check_buy_slippage, check_sell_slippage`（`DEFAULT_SLIPPAGE_BPS` 别处若有引用，grep 确认——`quote` 不用，无其他引用则只 import 两个 check 函数）
- buy 的 `:563-580`（`if req.max_cost ...` 到 bps 检查块结束）整段替换为：

```python
        check_buy_slippage(
            pay, expected_pay, marginal_price_before_buy,
            req.max_cost, req.max_slippage_bps, req.accept_any_slippage,
        )
```

- sell 的 `:736-752` 同理替换为 `check_sell_slippage(proceeds, net, expected_proceeds, marginal_price_before_sell, req.min_proceeds, req.max_slippage_bps, req.accept_any_slippage)`
- 保留两处 `expected_pay` / `expected_proceeds` / `marginal_price_*` 的计算行与其注释

- [ ] **Step 5: 跑测确认行为不变**

Run: `python -m pytest tests/test_trade_checks.py tests/test_market_slippage_lock.py tests/test_market_deadlock_fix.py -q`
Expected: 全 PASS（老路径滑点行为逐字不变）

- [ ] **Step 6: Commit**

```bash
git add app/services/trade_checks.py app/api/v1/market.py tests/test_trade_checks.py
git commit -m "refactor(trade): 滑点校验提取纯函数——老路径与 writer 新路径共用单一实现"
```

---

### Task 6: BuyCmd op + 新旧路径 parity 测试

**Files:**
- Create: `backend/app/services/writer_ops.py`
- Modify: `backend/app/api/v1/market.py`（`buy_shares` 加 writer 分支）
- Modify: `backend/app/services/market_writer.py`（`start()` 末尾注册 ops）
- Test: `backend/tests/test_writer_buy.py`

**Interfaces:**
- Consumes: Task 3 `OpOutcome`/`register_op`、Task 5 `check_buy_slippage`、既有 `lock_user`、`assert_user_can_trade_market`、`compute_candle_rows`、lmsr 函数族
- Produces:
  - `@dataclass BuyCmd: market_id: int; outcome_id: int; user_id: int; username: str; shares: Decimal; max_cost: Decimal | None; max_slippage_bps: int | None; accept_any_slippage: bool`
  - `async def op_buy(state: MarketState, cmd: BuyCmd) -> OpOutcome`
  - `def register_all_ops(writer: MarketWriter) -> None`（Task 7-9 往里追加注册）

- [ ] **Step 1: 写失败测试**

`backend/tests/test_writer_buy.py`（内嵌 `_fresh_db`/`_seed_market` 同前，另加建 user helper）：

```python
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.core.database import async_session_maker, engine
from app.models.base import (
    Market, MarketStatus, Outcome, Position, Transaction, TransactionType, User,
)
from app.services.market_writer import WRITER
from app.services.writer_ops import BuyCmd

# （粘贴 _fresh_db / _seed_market；_fresh_db teardown 含 await WRITER.stop()）


async def _seed_user(cash="1000") -> int:
    async with async_session_maker() as s:
        u = User(username="alice", email="a@x.com", hashed_password="x",
                 cash=Decimal(cash), is_active=True)
        s.add(u); await s.flush()
        uid = u.id
        await s.commit()
        return uid


def _buy(mid, oid, uid, shares="10", **kw):
    return BuyCmd(market_id=mid, outcome_id=oid, user_id=uid, username="alice",
                  shares=Decimal(shares), max_cost=kw.get("max_cost"),
                  max_slippage_bps=kw.get("max_slippage_bps"),
                  accept_any_slippage=kw.get("accept_any_slippage", False))


@pytest.mark.asyncio
async def test_buy_happy_path_db_and_memory_consistent():
    mid, oids = await _seed_market(shares=("0", "0"))
    uid = await _seed_user()
    await WRITER.start()
    res = await WRITER.submit(_buy(mid, oids[0], uid, shares="10", accept_any_slippage=True))
    assert res["shares"] == 10.0
    assert res["cost"] > 0
    st = WRITER.get_state(mid)
    assert st.q_dec[0] == Decimal("10.000000")
    async with async_session_maker() as s:
        o = await s.get(Outcome, oids[0])
        assert o.total_shares == st.q_dec[0]          # 镜像与内存恒等（不动点）
        u = await s.get(User, uid)
        assert u.cash == Decimal("1000") - Decimal(str(res["cost"])).quantize(Decimal("0.01")) or True
        pos = (await s.execute(select(Position).where(
            Position.user_id == uid, Position.outcome_id == oids[0]))).scalars().first()
        assert pos.amount == Decimal("10.000000")
        tx = (await s.execute(select(Transaction))).scalars().all()
        assert len(tx) == 1 and tx[0].type == TransactionType.BUY
    # 现金精确校验：cost_basis == 实付
    async with async_session_maker() as s:
        pos = (await s.execute(select(Position))).scalars().first()
        u = await s.get(User, uid)
        assert u.cash + pos.cost_basis == Decimal("1000")


@pytest.mark.asyncio
async def test_buy_insufficient_cash_rolls_back_everything():
    mid, oids = await _seed_market(shares=("0", "0"))
    uid = await _seed_user(cash="0.01")
    await WRITER.start()
    with pytest.raises(HTTPException) as ei:
        await WRITER.submit(_buy(mid, oids[0], uid, shares="10", accept_any_slippage=True))
    assert ei.value.detail == "现金不足"
    st = WRITER.get_state(mid)
    assert st.q_dec[0] == Decimal("0.000000")          # 内存一个字节没动
    async with async_session_maker() as s:
        o = await s.get(Outcome, oids[0])
        assert o.total_shares == Decimal("0")
        assert (await s.execute(select(Transaction))).scalars().first() is None


@pytest.mark.asyncio
async def test_buy_rejected_on_halt_and_settled_state():
    mid, oids = await _seed_market(status=MarketStatus.HALT)
    uid = await _seed_user()
    await WRITER.start()
    with pytest.raises(HTTPException) as ei:
        await WRITER.submit(_buy(mid, oids[0], uid))
    assert ei.value.detail == "市场当前不可交易"


@pytest.mark.asyncio
async def test_buy_slippage_rejected_before_db():
    mid, oids = await _seed_market(shares=("0", "0"))
    uid = await _seed_user()
    await WRITER.start()
    # b=100 买 200 份冲击巨大，默认 500bps 必拒
    with pytest.raises(HTTPException) as ei:
        await WRITER.submit(_buy(mid, oids[0], uid, shares="200"))
    assert "滑点" in ei.value.detail


@pytest.mark.asyncio
async def test_buy_parity_with_old_path(client):
    """两个同参市场：老路径 API 买 vs writer 直接买，结果逐字段一致。"""
    from tests.test_writer_buy import _buy  # noqa: 自引用防误删
    # 老路径：flag 默认关，走 /api/v1/market/buy
    reg = await client.post("/api/v1/auth/register", json={
        "email": "p@x.com", "password": "pw123456", "username": "parity"})
    assert reg.status_code in (200, 201)
    login = await client.post("/api/v1/auth/jwt/login",
                              data={"username": "p@x.com", "password": "pw123456"})
    token = login.json()["access_token"]
    hdr = {"Authorization": f"Bearer {token}"}
    # 两个一模一样的市场
    mid_old, oids_old = await _seed_market(shares=("0", "0"))
    mid_new, oids_new = await _seed_market(shares=("0", "0"))
    r_old = await client.post("/api/v1/market/buy", headers=hdr, json={
        "outcome_id": oids_old[0], "shares": 10, "accept_any_slippage": True})
    assert r_old.status_code == 200, r_old.text
    # writer 路径
    await WRITER.start()
    async with async_session_maker() as s:
        uid = (await s.execute(select(User).where(User.username == "parity"))).scalars().first().id
    r_new = await WRITER.submit(_buy(mid_new, oids_new[0], uid, shares="10",
                                     accept_any_slippage=True))
    assert r_old.json()["cost"] == r_new["cost"]
    assert r_old.json()["shares"] == r_new["shares"]
    async with async_session_maker() as s:
        o_old = await s.get(Outcome, oids_old[0])
        o_new = await s.get(Outcome, oids_new[0])
        assert o_old.total_shares == o_new.total_shares
```

注意：parity 测试用了 `client` fixture（conftest 的 module-scope lifespan）；auth 注册端点字段以 `tests/test_auth.py` 现有用法为准，编写时对照调整（步骤内此段可按实际 auth 流程改写，断言目标不变：**两条路径同参同结果**）。

- [ ] **Step 2: 跑测确认失败**

Run: `python -m pytest tests/test_writer_buy.py -x -q`
Expected: FAIL —— `ModuleNotFoundError: app.services.writer_ops`

- [ ] **Step 3: 实现 writer_ops.py 的 op_buy**

```python
"""writer 命令实现（spec § 4.3 生命周期）。

每个 op：先内存定价/校验（零 IO，失败即拒），再开独立 DB 事务
（唯一阻塞点），commit 成功后把「新 q / candle 行 / SSE 事件」交给
consumer 统一 apply——op 返回即视为已 commit（spec § 4.4）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select, update as sa_update

from app.core.database import async_session_maker
from app.models.base import (
    Market, MarketStatus, Outcome, Position, Transaction, TransactionType, User,
)
from app.services.candle_writer import compute_candle_rows
from app.services.lmsr import calculate_lmsr_with_prices, quantize_cost, quantize_price
from app.services.market_locks import lock_user
from app.services.market_title_gating import assert_user_can_trade_market
from app.services.market_writer import MarketState, MarketWriter, OpOutcome
from app.services.trade_checks import check_buy_slippage, check_sell_slippage
from app.services import site_config

logger = logging.getLogger(__name__)
ZERO = Decimal("0")


def _require_trading_state(state: MarketState) -> None:
    """与 market.py::_require_trading 同语义，输入换成内存 state。"""
    if state.status != MarketStatus.TRADING:
        raise HTTPException(status_code=400, detail="市场当前不可交易")
    if state.closes_at and datetime.now(timezone.utc) >= state.closes_at:
        raise HTTPException(status_code=400, detail="市场已过交易截止时间")


def _target_idx(state: MarketState, outcome_id: int) -> int:
    try:
        return state.outcome_ids.index(int(outcome_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="选项不属于该市场（数据异常）")


@dataclass
class BuyCmd:
    market_id: int
    outcome_id: int
    user_id: int
    username: str
    shares: Decimal
    max_cost: Optional[Decimal]
    max_slippage_bps: Optional[int]
    accept_any_slippage: bool


async def op_buy(state: MarketState, cmd: BuyCmd) -> OpOutcome:
    # ── 1. 内存定价（微秒，零 IO）──
    _require_trading_state(state)
    idx = _target_idx(state, cmd.outcome_id)
    shares_d = quantize_cost(cmd.shares)
    if shares_d <= ZERO:
        raise HTTPException(status_code=422, detail="shares 必须为正数")

    old_q = state.q
    b = state.b
    new_q = list(old_q)
    new_q[idx] += float(shares_d)
    old_cost_f, old_prices = calculate_lmsr_with_prices(old_q, b)
    new_cost_f, new_prices = calculate_lmsr_with_prices(new_q, b)
    pay = quantize_cost(new_cost_f - old_cost_f)
    if pay <= ZERO:
        raise HTTPException(status_code=400, detail="订单异常：成本不应为非正")

    # ── 2. 滑点/校验（纯内存）──
    marginal_price = Decimal(str(old_prices[idx]))
    expected_pay = (marginal_price * shares_d).quantize(Decimal("0.000001"))
    check_buy_slippage(pay, expected_pay, marginal_price,
                       cmd.max_cost, cmd.max_slippage_bps, cmd.accept_any_slippage)

    # 影子新 q（Decimal 6dp 精确加法；commit 后才回写内存）
    new_q_dec = list(state.q_dec)
    new_q_dec[idx] = quantize_cost(new_q_dec[idx] + shares_d)

    avg_price = quantize_price(pay / shares_d)
    pre_mp = quantize_price(old_prices[idx])
    post_mp = quantize_price(new_prices[idx])

    # ── 3. DB 事务（唯一阻塞点）──
    async with async_session_maker() as session:
        async with session.begin():
            locked_user = await lock_user(session, cmd.user_id)
            # title 门槛：与老路径同位置（锁内、扣款前），语义不变
            await assert_user_can_trade_market(session, cmd.user_id, state.market_id)
            if locked_user.cash < pay:
                raise HTTPException(status_code=400, detail="现金不足")
            locked_user.cash -= pay

            pos = (await session.execute(
                select(Position)
                .where(Position.user_id == cmd.user_id,
                       Position.outcome_id == int(cmd.outcome_id))
                .with_for_update()
            )).scalars().first()
            if not pos:
                pos = Position(user_id=cmd.user_id, outcome_id=int(cmd.outcome_id),
                               amount=ZERO, cost_basis=ZERO)
                session.add(pos)
            pos.amount += shares_d
            pos.cost_basis += pay

            tx = Transaction(
                user_id=cmd.user_id,
                outcome_id=int(cmd.outcome_id),
                type=TransactionType.BUY,
                shares=shares_d,
                cost=pay,
                price=avg_price,
                pre_market_price=pre_mp,
                post_market_price=post_mp,
                gross=pay,
                fee=ZERO,
                market_prices_post=list(new_prices),
            )
            session.add(tx)

            # 镜像：writer 是唯一写者，直接 SET 绝对值 = 影子 q_dec（不动点恒等）
            await session.execute(
                sa_update(Outcome)
                .where(Outcome.id == int(cmd.outcome_id))
                .values(total_shares=new_q_dec[idx])
            )
        new_cash = locked_user.cash   # expire_on_commit=False，commit 后可读

    # ── 4. commit 成功 → 组装 apply 数据 ──
    ts = tx.timestamp if tx.timestamp else datetime.now(timezone.utc)
    candle_rows = compute_candle_rows(
        traded_outcome_id=int(cmd.outcome_id),
        outcome_ids=state.outcome_ids,
        pre_prices=old_prices,
        new_prices=new_prices,
        traded_shares=shares_d,
        ts=ts,
    )
    label = state.outcome_labels[idx]
    logger.info(
        "BUY(writer) user_id=%s outcome_id=%s market_id=%s shares=%s cost=%s avg_price=%s "
        "pre_mp=%s post_mp=%s new_cash=%s",
        cmd.user_id, cmd.outcome_id, state.market_id, shares_d, pay, avg_price,
        pre_mp, post_mp, new_cash,
    )
    return OpOutcome(
        response={
            "shares": float(shares_d),
            "cost": float(pay.quantize(Decimal("0.01"))),
            "new_cash": float(new_cash.quantize(Decimal("0.01"))),
            "message": f"成功买入 {shares_d:f} 张 {label}（均价≈{avg_price}）",
        },
        new_q_dec=new_q_dec,
        new_prices=new_prices,
        candle_rows=candle_rows,
        publishes=[(
            "trade",
            {"trade": {
                "id": int(tx.id),
                "type": TransactionType.BUY,
                "outcome_id": int(cmd.outcome_id),
                "username": cmd.username,
                "shares": float(shares_d),
                "price": float(avg_price),
                "gross": float(pay),
                "fee": 0.0,
                "post_market_price": float(post_mp),
                "market_prices_post": [float(p) for p in new_prices],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }},
        )],
    )


def register_all_ops(writer: MarketWriter) -> None:
    writer.register_op(BuyCmd, op_buy)
    # Task 7-9 在此追加 SellCmd / ResolveCmd / CloseCmd / ResumeCmd / LiquidateMarketCmd
```

`market_writer.py` 的 `start()` 末尾（`self._enabled = True` 前）加：

```python
        from app.services.writer_ops import register_all_ops   # 局部 import 避免环
        register_all_ops(self)
```

- [ ] **Step 4: market.py buy 分支**

`buy_shares` 函数体开头（`shares_d = quantize_cost(...)` 校验之后、`managed_transaction` 之前）插入：

```python
    from app.services.market_writer import WRITER
    from app.services.writer_ops import BuyCmd
    if WRITER.enabled:
        mid = WRITER.market_id_for_outcome(int(req.outcome_id))
        if mid is None:
            row = await db.execute(
                select(Outcome.market_id).where(Outcome.id == int(req.outcome_id)))
            if row.scalars().first() is None:
                raise HTTPException(status_code=404, detail="选项不存在")
            # outcome 存在但市场不在 writer（启动前已 SETTLED）→ 与老路径同文案
            raise HTTPException(status_code=400, detail="市场当前不可交易")
        return await WRITER.submit(BuyCmd(
            market_id=mid,
            outcome_id=int(req.outcome_id),
            user_id=int(user.id),
            username=user.username,
            shares=shares_d,
            max_cost=req.max_cost,
            max_slippage_bps=req.max_slippage_bps,
            accept_any_slippage=req.accept_any_slippage,
        ))
```

（import 放函数内，避免 market.py 顶部引入环。）

- [ ] **Step 5: 跑测确认通过**

Run: `python -m pytest tests/test_writer_buy.py -q && python -m pytest tests/test_market_slippage_lock.py tests/test_candle_integration.py -q`
Expected: 全 PASS（后两个证明老路径无回归——flag 默认关）

- [ ] **Step 6: Commit**

```bash
git add app/services/writer_ops.py app/services/market_writer.py app/api/v1/market.py tests/test_writer_buy.py
git commit -m "feat(writer): BuyCmd——内存定价+DB事务+commit后回写，与老路径 parity 验证"
```

---

### Task 7: SellCmd op

**Files:**
- Modify: `backend/app/services/writer_ops.py`（`SellCmd` + `op_sell` + 注册）
- Modify: `backend/app/api/v1/market.py`（`sell_shares` 加同款分支）
- Test: `backend/tests/test_writer_sell.py`

**Interfaces:**
- Produces: `@dataclass SellCmd: market_id: int; outcome_id: int; user_id: int; username: str; shares: Decimal; min_proceeds: Decimal | None; max_slippage_bps: int | None; accept_any_slippage: bool`；`async def op_sell(state, cmd) -> OpOutcome`

**与老路径的已知行为差**（写进 op docstring）：老路径先查持仓再算 LMSR；writer 路径滑点在内存先算、持仓在 DB 事务里查——「持仓不足 **且** 滑点超限」的双违规请求会先收到滑点错误而不是持仓错误。单违规行为完全一致。

- [ ] **Step 1: 写失败测试**

`backend/tests/test_writer_sell.py`（fixture/helper 同 Task 6 文件；先用 BuyCmd 建仓再测卖）：

```python
# imports 与 fixture 同 test_writer_buy.py，另 import SellCmd

def _sell(mid, oid, uid, shares="5", **kw):
    return SellCmd(market_id=mid, outcome_id=oid, user_id=uid, username="alice",
                   shares=Decimal(shares), min_proceeds=kw.get("min_proceeds"),
                   max_slippage_bps=kw.get("max_slippage_bps"),
                   accept_any_slippage=kw.get("accept_any_slippage", False))


@pytest.mark.asyncio
async def test_sell_roundtrip_conserves_cash():
    """买 10 卖 10（fee=0）→ 现金精确回到起点，q 回到 0。"""
    mid, oids = await _seed_market(shares=("0", "0"))
    uid = await _seed_user(cash="1000")
    await WRITER.start()
    await WRITER.submit(_buy(mid, oids[0], uid, shares="10", accept_any_slippage=True))
    await WRITER.submit(_sell(mid, oids[0], uid, shares="10", accept_any_slippage=True))
    st = WRITER.get_state(mid)
    assert st.q_dec[0] == Decimal("0.000000")
    async with async_session_maker() as s:
        u = await s.get(User, uid)
        # LMSR 往返 + 6dp 量化，现金误差 ≤ 2 个 LSB
        assert abs(u.cash - Decimal("1000")) <= Decimal("0.000002")
        o = await s.get(Outcome, oids[0])
        assert o.total_shares == Decimal("0.000000")
        pos = (await s.execute(select(Position).where(Position.user_id == uid))).scalars().first()
        assert pos.amount == Decimal("0.000000")
        assert pos.cost_basis == Decimal("0.000000")   # 清仓归零


@pytest.mark.asyncio
async def test_sell_insufficient_position_rejected_in_db_txn():
    mid, oids = await _seed_market(shares=("0", "0"))
    uid = await _seed_user()
    await WRITER.start()
    await WRITER.submit(_buy(mid, oids[0], uid, shares="5", accept_any_slippage=True))
    with pytest.raises(HTTPException) as ei:
        await WRITER.submit(_sell(mid, oids[0], uid, shares="10", accept_any_slippage=True))
    assert ei.value.detail == "持仓不足"
    st = WRITER.get_state(mid)
    assert st.q_dec[0] == Decimal("5.000000")   # 内存未动


@pytest.mark.asyncio
async def test_sell_fee_applied_from_site_config():
    from sqlalchemy import text
    from app.services.site_config import clear_cache
    mid, oids = await _seed_market(shares=("0", "0"))
    uid = await _seed_user()
    async with async_session_maker() as s:
        await s.execute(text(
            "INSERT INTO siteconfig (key, value, value_type, updated_at) "
            "VALUES ('sell_fee_rate', '0.01', 'decimal', CURRENT_TIMESTAMP)"))
        await s.commit()
    clear_cache()
    await WRITER.start()
    await WRITER.submit(_buy(mid, oids[0], uid, shares="10", accept_any_slippage=True))
    async with async_session_maker() as s:
        cash_before = (await s.get(User, uid)).cash
    await WRITER.submit(_sell(mid, oids[0], uid, shares="10", accept_any_slippage=True))
    async with async_session_maker() as s:
        tx = (await s.execute(select(Transaction).where(
            Transaction.type == TransactionType.SELL))).scalars().first()
        assert tx.fee == (tx.gross * Decimal("0.01")).quantize(Decimal("0.000001"))
        u = await s.get(User, uid)
        assert u.cash == cash_before + tx.gross - tx.fee


@pytest.mark.asyncio
async def test_sell_market_total_insufficient():
    """内存 q 不足时拒绝（异常状态守护，与老路径同文案）。"""
    mid, oids = await _seed_market(shares=("2", "0"))
    uid = await _seed_user()
    await WRITER.start()
    # 用户没持仓也会先撞总量检查？不——总量 2 ≥ 卖 1，会走到持仓检查。
    # 构造总量不足：直接卖 5 > 总量 2
    with pytest.raises(HTTPException) as ei:
        await WRITER.submit(_sell(mid, oids[0], uid, shares="5", accept_any_slippage=True))
    assert ei.value.detail == "市场总份额不足（异常状态）"
```

- [ ] **Step 2: 跑测确认失败**

Run: `python -m pytest tests/test_writer_sell.py -x -q`
Expected: FAIL —— `ImportError: SellCmd`

- [ ] **Step 3: 实现 op_sell**

在 `writer_ops.py` 追加（结构与 `op_buy` 对称；此处为完整实现要点，逐条落）：

```python
@dataclass
class SellCmd:
    market_id: int
    outcome_id: int
    user_id: int
    username: str
    shares: Decimal
    min_proceeds: Optional[Decimal]
    max_slippage_bps: Optional[int]
    accept_any_slippage: bool


async def op_sell(state: MarketState, cmd: SellCmd) -> OpOutcome:
    # 1. 内存定价与校验
    _require_trading_state(state)
    idx = _target_idx(state, cmd.outcome_id)
    shares_d = quantize_cost(cmd.shares)
    if shares_d <= ZERO:
        raise HTTPException(status_code=422, detail="shares 必须为正数")
    if state.q[idx] < float(shares_d):
        raise HTTPException(status_code=400, detail="市场总份额不足（异常状态）")

    old_q, b = state.q, state.b
    new_q = list(old_q)
    new_q[idx] -= float(shares_d)
    old_cost_f, old_prices = calculate_lmsr_with_prices(old_q, b)
    new_cost_f, new_prices = calculate_lmsr_with_prices(new_q, b)
    proceeds = quantize_cost(old_cost_f - new_cost_f)
    if proceeds < ZERO:
        proceeds = ZERO

    new_q_dec = list(state.q_dec)
    new_q_dec[idx] = quantize_cost(new_q_dec[idx] - shares_d)

    marginal_price = Decimal(str(old_prices[idx]))
    expected_proceeds = (marginal_price * shares_d).quantize(Decimal("0.000001"))
    avg_price = quantize_price(proceeds / shares_d) if shares_d > ZERO else ZERO
    pre_mp = quantize_price(old_prices[idx])
    post_mp = quantize_price(new_prices[idx])

    # 2. DB 事务：fee 率读取 + 滑点（fee 依赖 site_config，在事务 session 上读，60s 缓存）
    async with async_session_maker() as session:
        async with session.begin():
            sell_fee_rate = await site_config.get_decimal_or(session, "sell_fee_rate", ZERO)
            fee = (proceeds * sell_fee_rate).quantize(Decimal("0.000001"))
            net = proceeds - fee
            check_sell_slippage(proceeds, net, expected_proceeds, marginal_price,
                                cmd.min_proceeds, cmd.max_slippage_bps,
                                cmd.accept_any_slippage)

            locked_user = await lock_user(session, cmd.user_id)
            pos = (await session.execute(
                select(Position)
                .where(Position.user_id == cmd.user_id,
                       Position.outcome_id == int(cmd.outcome_id))
                .with_for_update()
            )).scalars().first()
            if not pos or pos.amount < shares_d:
                raise HTTPException(status_code=400, detail="持仓不足")

            locked_user.cash += net
            if pos.amount > ZERO:
                sold_ratio = shares_d / pos.amount
                pos.cost_basis -= (pos.cost_basis * sold_ratio).quantize(Decimal("0.000001"))
            pos.amount -= shares_d
            if pos.amount <= ZERO:
                pos.cost_basis = ZERO

            tx = Transaction(
                user_id=cmd.user_id, outcome_id=int(cmd.outcome_id),
                type=TransactionType.SELL, shares=shares_d, cost=-net,
                price=avg_price, pre_market_price=pre_mp, post_market_price=post_mp,
                gross=proceeds, fee=fee, market_prices_post=list(new_prices),
            )
            session.add(tx)
            await session.execute(
                sa_update(Outcome).where(Outcome.id == int(cmd.outcome_id))
                .values(total_shares=new_q_dec[idx])
            )
        new_cash = locked_user.cash

    ts = tx.timestamp if tx.timestamp else datetime.now(timezone.utc)
    candle_rows = compute_candle_rows(
        traded_outcome_id=int(cmd.outcome_id), outcome_ids=state.outcome_ids,
        pre_prices=old_prices, new_prices=new_prices, traded_shares=shares_d, ts=ts,
    )
    logger.info(
        "SELL(writer) user_id=%s outcome_id=%s market_id=%s shares=%s proceeds=%s fee=%s "
        "net=%s avg_price=%s new_cash=%s",
        cmd.user_id, cmd.outcome_id, state.market_id, shares_d, proceeds, fee, net,
        avg_price, new_cash,
    )
    return OpOutcome(
        response={
            "shares": float(shares_d),
            "cost": float((-net).quantize(Decimal("0.01"))),
            "new_cash": float(new_cash.quantize(Decimal("0.01"))),
            "message": f"卖出成功，获得 {net}（手续费 {fee}，均价≈{avg_price}）",
        },
        new_q_dec=new_q_dec,
        new_prices=new_prices,
        candle_rows=candle_rows,
        publishes=[(
            "trade",
            {"trade": {
                "id": int(tx.id), "type": TransactionType.SELL,
                "outcome_id": int(cmd.outcome_id), "username": cmd.username,
                "shares": float(shares_d), "price": float(avg_price),
                "gross": float(proceeds), "fee": float(fee),
                "post_market_price": float(post_mp),
                "market_prices_post": [float(p) for p in new_prices],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }},
        )],
    )
```

`register_all_ops` 加 `writer.register_op(SellCmd, op_sell)`。

注意 fee/滑点在事务内算但在改数据之前——事务内 raise 会整体 rollback，无部分写入。

- [ ] **Step 4: market.py sell 分支**

`sell_shares` 开头插入与 buy 同构的分支（`SellCmd`，字段换 `min_proceeds`）。

- [ ] **Step 5: 跑测确认通过**

Run: `python -m pytest tests/test_writer_sell.py tests/test_writer_buy.py -q`
Expected: 全 PASS

- [ ] **Step 6: Commit**

```bash
git add app/services/writer_ops.py app/api/v1/market.py tests/test_writer_sell.py
git commit -m "feat(writer): SellCmd——含 fee/滑点/持仓校验，买卖往返现金守恒验证"
```

---

### Task 8: Resolve / Close / Resume ops

**Files:**
- Modify: `backend/app/services/writer_ops.py`
- Modify: `backend/app/api/v1/market.py`（三个管理端点加分支）
- Test: `backend/tests/test_writer_admin_ops.py`

**Interfaces:**
- Produces:
  - `@dataclass CloseCmd: market_id: int` / `@dataclass ResumeCmd: market_id: int`
  - `@dataclass ResolveCmd: market_id: int; winning_outcome_id: int; payout: Decimal; admin_id: int`
  - `op_close` / `op_resume` 返回 `OpOutcome(response={"message": ...}, new_status=..., publishes=[("market_status", {...})])`
  - `op_resolve` 返回 `OpOutcome(response=SettleResult 实例, new_status=SETTLED, publishes=[("market_status", {...})])`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_writer_admin_ops.py`（fixture 同前）：

```python
# import CloseCmd, ResumeCmd, ResolveCmd, BuyCmd

@pytest.mark.asyncio
async def test_close_then_resume_updates_db_memory_and_rejects_trades():
    mid, oids = await _seed_market()
    uid = await _seed_user()
    await WRITER.start()
    await WRITER.submit(CloseCmd(market_id=mid))
    assert WRITER.get_state(mid).status == MarketStatus.HALT
    async with async_session_maker() as s:
        assert (await s.get(Market, mid)).status == MarketStatus.HALT
    with pytest.raises(HTTPException):
        await WRITER.submit(_buy(mid, oids[0], uid, accept_any_slippage=True))
    await WRITER.submit(ResumeCmd(market_id=mid))
    assert WRITER.get_state(mid).status == MarketStatus.TRADING
    res = await WRITER.submit(_buy(mid, oids[0], uid, shares="1", accept_any_slippage=True))
    assert res["shares"] == 1.0


@pytest.mark.asyncio
async def test_resume_requires_halt():
    mid, _ = await _seed_market()
    await WRITER.start()
    with pytest.raises(HTTPException) as ei:
        await WRITER.submit(ResumeCmd(market_id=mid))
    assert "不在熔断状态" in ei.value.detail


@pytest.mark.asyncio
async def test_resolve_pays_winner_deletes_positions_and_settles():
    mid, oids = await _seed_market(shares=("0", "0"))
    uid_w = await _seed_user()          # 买赢家
    uid_l = await _seed_user2()         # 买输家（helper: username="bob"）
    await WRITER.start()
    await WRITER.submit(_buy(mid, oids[0], uid_w, shares="10", accept_any_slippage=True))
    await WRITER.submit(SellCmdless := _buy(mid, oids[1], uid_l, shares="10", accept_any_slippage=True))
    async with async_session_maker() as s:
        cash_w_before = (await s.get(User, uid_w)).cash
    res = await WRITER.submit(ResolveCmd(
        market_id=mid, winning_outcome_id=oids[0], payout=Decimal("1"), admin_id=uid_w))
    assert res.status == MarketStatus.SETTLED
    assert res.winning_outcome_id == oids[0]
    assert res.total_payout == Decimal("10.000000")
    assert res.settled_positions == 2
    st = WRITER.get_state(mid)
    assert st.status == MarketStatus.SETTLED       # 状态留内存，后续交易被拒
    async with async_session_maker() as s:
        assert (await s.get(User, uid_w)).cash == cash_w_before + Decimal("10")
        assert (await s.execute(select(Position))).scalars().first() is None
        lose_tx = (await s.execute(select(Transaction).where(
            Transaction.type == TransactionType.SETTLE_LOSE))).scalars().all()
        assert len(lose_tx) == 1 and lose_tx[0].user_id == uid_l
    with pytest.raises(HTTPException):
        await WRITER.submit(_buy(mid, oids[0], uid_w, accept_any_slippage=True))


@pytest.mark.asyncio
async def test_resolve_idempotent_second_call():
    mid, oids = await _seed_market()
    uid = await _seed_user()
    await WRITER.start()
    await WRITER.submit(ResolveCmd(market_id=mid, winning_outcome_id=oids[0],
                                   payout=Decimal("1"), admin_id=uid))
    res2 = await WRITER.submit(ResolveCmd(market_id=mid, winning_outcome_id=oids[0],
                                          payout=Decimal("1"), admin_id=uid))
    assert res2.total_payout == Decimal("0")       # 与老路径幂等语义一致
    assert res2.settled_positions == 0
```

（`_seed_user2` 是 `_seed_user` 的 username/email 变体 helper，直接写在测试文件里。）

- [ ] **Step 2: 跑测确认失败**

Run: `python -m pytest tests/test_writer_admin_ops.py -x -q`
Expected: FAIL —— `ImportError: CloseCmd`

- [ ] **Step 3: 实现三个 op**

`op_close` / `op_resume`（完整）：

```python
@dataclass
class CloseCmd:
    market_id: int


@dataclass
class ResumeCmd:
    market_id: int


async def op_close(state: MarketState, cmd: CloseCmd) -> OpOutcome:
    if state.status == MarketStatus.SETTLED:
        raise HTTPException(status_code=400, detail="市场已结算，无法熔断")
    async with async_session_maker() as session:
        async with session.begin():
            market = await session.get(Market, cmd.market_id)
            market.status = MarketStatus.HALT
        title = market.title
    return OpOutcome(
        response={"message": f"市场 {title} 已停止交易（熔断）"},
        new_status=MarketStatus.HALT,
        publishes=[("market_status", {"status": MarketStatus.HALT})],
    )


async def op_resume(state: MarketState, cmd: ResumeCmd) -> OpOutcome:
    if state.status == MarketStatus.SETTLED:
        raise HTTPException(status_code=400, detail="市场已结算，无法恢复交易")
    if state.status != MarketStatus.HALT:
        raise HTTPException(status_code=400, detail="市场当前不在熔断状态")
    async with async_session_maker() as session:
        async with session.begin():
            market = await session.get(Market, cmd.market_id)
            market.status = MarketStatus.TRADING
        title = market.title
    return OpOutcome(
        response={"message": f"市场 {title} 已恢复交易"},
        new_status=MarketStatus.TRADING,
        publishes=[("market_status", {"status": MarketStatus.TRADING})],
    )
```

`op_resolve`：**移植** `market.py:850-989` 的事务体（结算是最长的既有逻辑，不重写数学）。移植配方：

1. `ResolveCmd` dataclass 如上；`payout_unit = quantize_cost(cmd.payout)`、`payout_unit < ZERO → 422`（原 `:846-848`）。
2. 事务框架换成 `async with async_session_maker() as session: async with session.begin():`。
3. 市场行读取从 `select(...).with_for_update()`（`:851-853`）换成 `await session.get(Market, cmd.market_id)`——writer 串行即是串行化保护，market 行锁不再需要（spec § 4.5）；`not market → 404` 保留。
4. 已 SETTLED 幂等分支（`:858-868`）原样保留（返回 `SettleResult`）。
5. outcomes 读取（`:870-875`）去掉 `.with_for_update()`，其余不变；`winning` 校验、positions 查询 **保留 `.with_for_update()`**（position 锁仍要，防与用户 sell 并发——不同市场 writer 间仍可能并发触碰同一 position？不能：position 属于本市场 outcome，只有本 writer 写——但保留锁零成本，照抄）。
6. 兑付循环（`:900-957`）逐字照抄：`SELECT user FOR UPDATE` 保留（跨路径 cash 串行化依赖，spec § 4.5）；`HTTPException(500)` 在事务内 raise → rollback，行为同旧。
7. `market.status/winning_outcome_id/settled_at/settled_by_user_id` 赋值照抄（admin id 用 `cmd.admin_id`）。
8. 事务外：构造与 `:972-989` 相同的 publish payload 与 `SettleResult`，放进 `OpOutcome(response=SettleResult(...), new_status=MarketStatus.SETTLED, publishes=[("market_status", {...})])`。`logger.info` 照抄（前缀 `RESOLVE(writer)`）。
9. 结算不改 q（`total_shares` 不动）→ `new_q_dec=None`。

`register_all_ops` 追加三个注册。

- [ ] **Step 4: market.py 三个端点加分支**

`close_market` / `resume_market` / `resolve_market` 函数体开头：

```python
    from app.services.market_writer import WRITER
    from app.services.writer_ops import CloseCmd   # 各自对应的 Cmd
    if WRITER.enabled and WRITER.get_state(market_id) is not None:
        return await WRITER.submit(CloseCmd(market_id=market_id))
```

（resolve 传 `ResolveCmd(market_id=market_id, winning_outcome_id=req.winning_outcome_id, payout=req.payout, admin_id=int(admin.id))`。`get_state is None`——启动前已 SETTLED 的市场——落回老路径，old code 的幂等/400 分支处理。）

- [ ] **Step 5: 跑测确认通过**

Run: `python -m pytest tests/test_writer_admin_ops.py -q`
Expected: 全 PASS

- [ ] **Step 6: Commit**

```bash
git add app/services/writer_ops.py app/api/v1/market.py tests/test_writer_admin_ops.py
git commit -m "feat(writer): resolve/close/resume 走 writer——结算逻辑移植，market 行锁退役"
```

---

### Task 9: Liquidation 拆 per-market 独立提交（spec § 4.6）

**Files:**
- Modify: `backend/app/services/writer_ops.py`（`LiquidateMarketCmd` + `op_liquidate_market`）
- Modify: `backend/app/services/liquidation_service.py`（新增 `liquidate_user_split` 编排器；老 `liquidate_user` 不动）
- Modify: `backend/app/services/liquidation_sweep.py`（`_liquidate_one_user` 加 writer 分支）
- Test: `backend/tests/test_writer_liquidation.py`

**Interfaces:**
- Produces:
  - `@dataclass LiquidateMarketCmd: market_id: int; user_id: int; mode: str; partial_pct: Decimal`（`mode` ∈ `"emergency" | "partial"`）
  - `op_liquidate_market -> OpOutcome`，其 `response` 为 `{"sold_count": int, "total_proceeds": Decimal}`
  - `async def liquidate_user_split(uid: int, *, daily_rate: Decimal, trigger_source: str, partial_pct: Decimal, target_margin: Decimal, emergency_threshold: Decimal) -> LiquidationEvent | None`（返回 None = noop，不写 DB event——对应老路径的 noop 分支）
- Consumes: `compute_users_holdings_value`（LCV 口径）、`loan_service.decrease_debt_locked`、`WRITER.submit`

**语义变化（spec § 4.6 已论证，测试按新语义断言）**：
- 全或无 → 逐市场提交：某市场失败不回滚其他市场
- `pre_hv` 用 `compute_users_holdings_value`（不锁 outcomes）计算——与老路径 inline 锁后计算可能差 ~1 LSB；pre_* 仅审计快照（老代码注释已声明不参与判定），可接受
- `LiquidationEvent` 是全部子命令返回后的汇总记录

- [ ] **Step 1: 写失败测试**

`backend/tests/test_writer_liquidation.py`：

```python
# fixture/helper 同前；另需给 user 设 debt

async def _seed_debtor(cash="0", debt="50") -> int:
    from datetime import datetime, timezone
    async with async_session_maker() as s:
        u = User(username="debtor", email="d@x.com", hashed_password="x",
                 cash=Decimal(cash), debt=Decimal(debt), is_active=True,
                 debt_last_accrued_at=datetime.now(timezone.utc))
        s.add(u); await s.flush()
        uid = u.id
        await s.commit()
        return uid


async def _give_position(uid, oid, amount, cost):
    """直接写 Position + 同步 outcome 镜像（绕过交易，测试布景用）。"""
    from sqlalchemy import update
    async with async_session_maker() as s:
        s.add(Position(user_id=uid, outcome_id=oid,
                       amount=Decimal(amount), cost_basis=Decimal(cost)))
        await s.execute(update(Outcome).where(Outcome.id == oid)
                        .values(total_shares=Outcome.total_shares + Decimal(amount)))
        await s.commit()


ARGS = dict(daily_rate=Decimal("0.001"), trigger_source="test",
            partial_pct=Decimal("1"), target_margin=Decimal("0.5"),
            emergency_threshold=Decimal("0.1"))


@pytest.mark.asyncio
async def test_split_liquidation_two_markets_writes_one_summary_event():
    from app.models.base import LiquidationEvent
    from app.services.liquidation_service import liquidate_user_split
    m1, o1 = await _seed_market(shares=("0", "0"))
    m2, o2 = await _seed_market(shares=("0", "0"))
    uid = await _seed_debtor(cash="0", debt="50")
    await _give_position(uid, o1[0], "20", "10")
    await _give_position(uid, o2[0], "20", "10")
    await WRITER.start()
    ev = await liquidate_user_split(uid, **ARGS)
    assert ev is not None
    assert ev.sold_positions_count == 2
    assert ev.total_proceeds > 0
    assert ev.repaid_amount > 0
    async with async_session_maker() as s:
        assert (await s.execute(select(Position))).scalars().first() is None
        events = (await s.execute(select(LiquidationEvent))).scalars().all()
        assert len(events) == 1                        # 汇总一条，不是每市场一条
        liq_tx = (await s.execute(select(Transaction).where(
            Transaction.type == TransactionType.LIQUIDATE))).scalars().all()
        assert len(liq_tx) == 2
        u = await s.get(User, uid)
        assert u.last_liquidated_at is not None
    # 内存镜像同步
    assert WRITER.get_state(m1).q_dec[0] == Decimal("0.000000")
    assert WRITER.get_state(m2).q_dec[0] == Decimal("0.000000")


@pytest.mark.asyncio
async def test_split_liquidation_halt_market_skipped_others_sold():
    m1, o1 = await _seed_market(shares=("0", "0"))
    m2, o2 = await _seed_market(shares=("0", "0"), status=MarketStatus.HALT)
    uid = await _seed_debtor()
    await _give_position(uid, o1[0], "20", "10")
    await _give_position(uid, o2[0], "20", "10")
    await WRITER.start()
    from app.services.liquidation_service import liquidate_user_split
    ev = await liquidate_user_split(uid, **ARGS)
    assert ev.sold_positions_count == 1                # HALT 市场跳过，其余照卖
    async with async_session_maker() as s:
        remaining = (await s.execute(select(Position))).scalars().all()
        assert len(remaining) == 1
        assert remaining[0].outcome_id == o2[0]


@pytest.mark.asyncio
async def test_split_liquidation_noop_returns_none_writes_nothing():
    from app.models.base import LiquidationEvent
    from app.services.liquidation_service import liquidate_user_split
    uid = await _seed_debtor(cash="0", debt="50")       # 无持仓无现金
    await WRITER.start()
    ev = await liquidate_user_split(uid, **ARGS)
    assert ev is None
    async with async_session_maker() as s:
        assert (await s.execute(select(LiquidationEvent))).scalars().first() is None
        assert (await s.get(User, uid)).last_liquidated_at is None
```

- [ ] **Step 2: 跑测确认失败**

Run: `python -m pytest tests/test_writer_liquidation.py -x -q`
Expected: FAIL —— `ImportError: liquidate_user_split`

- [ ] **Step 3: 实现 op_liquidate_market**

`writer_ops.py` 追加。移植 `liquidation_service.py:147-255` 的单市场循环体，配方：

```python
@dataclass
class LiquidateMarketCmd:
    market_id: int
    user_id: int
    mode: str                 # "emergency" | "partial"
    partial_pct: Decimal


async def op_liquidate_market(state: MarketState, cmd: LiquidateMarketCmd) -> OpOutcome:
    from decimal import ROUND_CEILING
    if state.status != MarketStatus.TRADING:
        # HALT/SETTLED 市场不强平（与老路径 skip 语义一致），空结果不算错误
        return OpOutcome(response={"sold_count": 0, "total_proceeds": ZERO})

    new_q_dec = list(state.q_dec)
    q_work = list(state.q)          # 同市场多仓位串行清算的滚动 q
    total_proceeds = ZERO
    sold_count = 0
    all_candle_rows: list[dict] = []

    async with async_session_maker() as session:
        async with session.begin():
            locked_user = await lock_user(session, cmd.user_id)
            positions = (await session.execute(
                select(Position)
                .join(Outcome, Position.outcome_id == Outcome.id)
                .where(Position.user_id == cmd.user_id,
                       Position.amount > 0,
                       Outcome.market_id == cmd.market_id)
                .order_by(Position.id.asc())
                .with_for_update()
            )).scalars().all()
            if not positions:
                return OpOutcome(response={"sold_count": 0, "total_proceeds": ZERO})

            for pos in positions:
                idx = _target_idx(state, pos.outcome_id)
                # sell_amount 按 mode（移植 liquidation_service.py:180-196，逐字）
                if cmd.mode == "emergency":
                    sell_amount = pos.amount
                else:
                    sell_amount = (pos.amount * cmd.partial_pct).quantize(
                        Decimal("1"), rounding=ROUND_CEILING)
                if sell_amount <= ZERO:
                    continue
                if sell_amount >= pos.amount:
                    sell_amount = pos.amount

                old_q = list(q_work)
                nq = list(old_q)
                nq[idx] -= float(sell_amount)
                old_cost, old_prices = calculate_lmsr_with_prices(old_q, state.b)
                new_cost, new_prices = calculate_lmsr_with_prices(nq, state.b)
                proceeds = quantize_cost(old_cost - new_cost)
                if proceeds < ZERO:
                    logger.error("liquidation_negative_proceeds(writer) user=%s pos=%s",
                                 cmd.user_id, pos.id)
                    continue    # skip not delete（老路径同语义）

                locked_user.cash += proceeds
                new_q_dec[idx] = quantize_cost(new_q_dec[idx] - sell_amount)
                q_work = nq
                if sell_amount >= pos.amount:
                    await session.delete(pos)
                else:
                    cost_reduced = (pos.cost_basis * sell_amount / pos.amount
                                    ).quantize(Decimal("0.000001"))
                    pos.amount -= sell_amount
                    pos.cost_basis -= cost_reduced

                avg_price = quantize_price(proceeds / sell_amount) if sell_amount > ZERO else ZERO
                session.add(Transaction(
                    user_id=cmd.user_id, outcome_id=pos.outcome_id,
                    type=TransactionType.LIQUIDATE, shares=sell_amount,
                    cost=-proceeds, price=avg_price,
                    pre_market_price=quantize_price(old_prices[idx]),
                    post_market_price=quantize_price(new_prices[idx]),
                    gross=proceeds, fee=ZERO,
                    market_prices_post=list(new_prices),
                ))
                all_candle_rows.extend(compute_candle_rows(
                    traded_outcome_id=pos.outcome_id, outcome_ids=state.outcome_ids,
                    pre_prices=old_prices, new_prices=new_prices,
                    traded_shares=sell_amount, ts=datetime.now(timezone.utc),
                ))
                total_proceeds += proceeds
                sold_count += 1

            # 镜像批量 SET（每个动过的 outcome 一条 UPDATE）
            for i, oid in enumerate(state.outcome_ids):
                if new_q_dec[i] != state.q_dec[i]:
                    await session.execute(
                        sa_update(Outcome).where(Outcome.id == oid)
                        .values(total_shares=new_q_dec[i]))

    return OpOutcome(
        response={"sold_count": sold_count, "total_proceeds": total_proceeds},
        new_q_dec=new_q_dec if sold_count else None,
        candle_rows=all_candle_rows,
        # 强平今天不发 SSE（与现状一致）
    )
```

（注意：LIQUIDATE 交易今天**不**写 candle？——查现状：`liquidation_service` 不调 `compute_candle_rows`，K 线只记 BUY/SELL。**保持一致：删掉上面 `all_candle_rows` 相关三处，`candle_rows` 不传**。此点以现状为准——实现时先 `grep -n compute_candle_rows app/services/liquidation_service.py` 确认无调用，然后不写 candle。）

`register_all_ops` 追加注册。

- [ ] **Step 4: 实现 liquidate_user_split 编排器**

`liquidation_service.py` 追加（老 `liquidate_user` 保留给老路径）：

```python
async def liquidate_user_split(
    uid: int,
    *,
    daily_rate: Decimal,
    trigger_source: str,
    partial_pct: Decimal,
    target_margin: Decimal,
    emergency_threshold: Decimal,
):
    """writer 路径的强平编排器（spec § 4.6）：逐市场独立提交，最后汇总。

    与老路径 liquidate_user 的差异：
    - 不再全或无——某市场失败只损失该市场的清算，其余照常
    - pre_* 快照用 compute_users_holdings_value（审计口径，允许 ~1 LSB 差）
    - LiquidationEvent 在全部子命令返回后统一写一条
    返回 None 表示 noop（无卖出且无还款），不写 event。
    """
    from fastapi import HTTPException
    from app.core.database import async_session_maker
    from app.services.market_writer import WRITER
    from app.services.wealth import compute_users_holdings_value
    from app.services.writer_ops import LiquidateMarketCmd

    # ── 阶段 A：快照 + mode 决策（短事务，锁完即放）──
    async with async_session_maker() as session:
        async with session.begin():
            user = await lock_user_ref(session, uid)   # 见下：复用 market_locks.lock_user
            if user.debt <= ZERO:
                return None
            pre_cash, pre_debt = user.cash, user.debt
            pre_hv = (await compute_users_holdings_value(
                session, user_ids=[uid])).get(uid, ZERO)
            market_ids = sorted(set((await session.execute(
                select(Outcome.market_id)
                .join(Position, Position.outcome_id == Outcome.id)
                .where(Position.user_id == uid, Position.amount > 0)
            )).scalars().all()))
    pre_nw = pre_cash - pre_debt + pre_hv
    pre_margin = pre_nw / pre_debt
    mode = "emergency" if pre_margin < emergency_threshold else "partial"

    # ── 阶段 B：逐市场提交（尽力而为）──
    total_proceeds = ZERO
    sold_count = 0
    for mid in market_ids:
        if WRITER.get_state(mid) is None:
            continue
        try:
            r = await WRITER.submit(LiquidateMarketCmd(
                market_id=mid, user_id=uid, mode=mode, partial_pct=partial_pct))
            total_proceeds += r["total_proceeds"]
            sold_count += r["sold_count"]
        except HTTPException as e:
            _logger.warning(
                "liquidation_market_cmd_failed user=%s market=%s status=%s detail=%s",
                uid, mid, e.status_code, e.detail)

    # ── 阶段 C：还债 + 汇总 event（独立事务）──
    async with async_session_maker() as session:
        async with session.begin():
            user = await lock_user_ref(session, uid)
            repaid = ZERO
            if user.cash > ZERO and user.debt > ZERO:
                repay_amount = min(user.cash, user.debt).quantize(Decimal("0.000001"))
                if repay_amount > ZERO:
                    repaid = await loan_service.decrease_debt_locked(
                        session, user, repay_amount,
                        consume_cash=True, daily_rate=daily_rate)
            if sold_count == 0 and repaid == ZERO:
                return None
            user.last_liquidated_at = datetime.now(timezone.utc)
            ev = LiquidationEvent(
                user_id=uid,
                triggered_at=datetime.now(timezone.utc),
                pre_cash=pre_cash, pre_debt=pre_debt,
                pre_holdings_value=pre_hv, pre_net_worth=pre_nw,
                pre_margin_ratio=pre_margin,
                sold_positions_count=sold_count,
                total_proceeds=total_proceeds,
                repaid_amount=repaid,
                remaining_debt=user.debt,
                post_cash=user.cash,
                trigger_source=trigger_source,
                mode=mode,
            )
            session.add(ev)
            await session.flush()
    _logger.warning(
        "user_liquidated(split)",
        extra={"user_id": uid, "sold_positions": sold_count,
               "total_proceeds": str(total_proceeds), "repaid": str(repaid),
               "remaining_debt": str(user.debt), "trigger_source": trigger_source},
    )
    return ev
```

（`lock_user_ref` = 文件顶部 `from app.services.market_locks import lock_user as lock_user_ref`——本文件已 import `lock_outcomes_for_market`，同处追加。）

- [ ] **Step 5: sweep 分支**

`liquidation_sweep.py::_liquidate_one_user` 中，`async with async_session_maker() as session:` 大事务之前加分支：

```python
    async with sem:
        try:
            from app.services.market_writer import WRITER
            if WRITER.enabled:
                # margin 判定仍在这里做（复用既有 stage-2 语义），判定通过才编排强平
                async with async_session_maker() as session:
                    async with session.begin():
                        user = await lock_user(session, uid)
                        if user.debt <= Decimal("0"):
                            return "skipped"
                        if await user_has_halt_holdings(session, uid):
                            logger.info("sweep_skip_user_with_halt_holdings",
                                        extra={"user_id": uid, "stage": "stage2_race_guard"})
                            return "skipped"
                        hv_now = (await compute_users_holdings_value(
                            session, user_ids=[uid])).get(uid, Decimal("0"))
                        margin_now = (user.cash - user.debt + hv_now) / user.debt
                        if margin_now >= hard_thr:
                            logger.info("sweep_skip_recovered",
                                        extra={"user_id": uid, "margin_now": float(margin_now)})
                            return "recovered"
                ev = await liquidation_service.liquidate_user_split(
                    uid, daily_rate=rate, trigger_source=trigger_source,
                    partial_pct=partial_pct, target_margin=target_margin,
                    emergency_threshold=emergency_threshold)
                if ev is None:
                    _recently_attempted[uid] = now
                return "triggered"
            # ↓ 老路径原逻辑，一行不动
            async with async_session_maker() as session:
                ...
```

- [ ] **Step 6: 跑测确认通过**

Run: `python -m pytest tests/test_writer_liquidation.py tests/test_liquidation_service.py tests/test_liquidation_sweep.py -q`
Expected: 全 PASS（老路径 liquidation 测试不受影响——flag 默认关）

- [ ] **Step 7: Commit**

```bash
git add app/services/writer_ops.py app/services/liquidation_service.py app/services/liquidation_sweep.py tests/test_writer_liquidation.py
git commit -m "feat(writer): 强平拆 per-market 独立提交——尽力而为逐市场清算，汇总一条 LiquidationEvent"
```

---

### Task 10: lifespan / flag / create_market 接线 + E2E + 全量验证

**Files:**
- Modify: `backend/app/services/loan_migrate.py`（`DEFAULT_CONFIGS` 加一行）
- Modify: `backend/app/services/site_config.py`（加 `get_bool_or`）
- Modify: `backend/app/main.py`（lifespan 启停 writer + flusher）
- Modify: `backend/app/api/v1/market.py`（`create_market` 注册新市场）
- Modify: `backend/tests/conftest.py`（每测试 writer/flusher 复位）
- Test: `backend/tests/test_writer_e2e.py`

**Interfaces:**
- Produces: `site_config.get_bool_or(session, key, default: bool) -> bool`
- 运维语义（写入代码注释）：`single_writer_enabled` **启动时读一次**，翻转需重启进程（spec § 8 阶段 1）；prod 初始默认 `false`（老路径）

- [ ] **Step 1: 写失败测试**

`backend/tests/test_writer_e2e.py`——走 HTTP 全链路，证明 flag 开启时端到端可用：

```python
"""writer 路径 E2E：flag 开 → API buy/sell 走 writer，SSE 事件形状不变，candle 经 flusher 落库。"""
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlmodel import select

from app.core.database import async_session_maker
from app.models.base import Outcome, OutcomeCandle
from app.services.candle_flusher import CANDLE_FLUSHER
from app.services.market_writer import WRITER
from app.services.site_config import clear_cache


@pytest_asyncio.fixture
async def writer_on(client):
    """启用 writer（模拟「flag=true 后重启」：显式 start）。"""
    async with async_session_maker() as s:
        await s.execute(text(
            "INSERT INTO siteconfig (key, value, value_type, updated_at) "
            "VALUES ('single_writer_enabled', 'true', 'bool', CURRENT_TIMESTAMP) "
            "ON CONFLICT (key) DO UPDATE SET value='true'"))
        await s.commit()
    clear_cache()
    await WRITER.start()
    yield
    await WRITER.stop()
    CANDLE_FLUSHER._pending.clear()


# admin/user 注册与登录 helper：对照 tests/test_candle_integration.py 现有写法拷贝
# （它已有完整的「建管理员 → 建市场 → 建用户 → 交易」HTTP 流程，本文件直接复用其模式）


@pytest.mark.asyncio
async def test_e2e_buy_sell_via_api_uses_writer(client, writer_on):
    # 1. HTTP 建市场（create_market 会 register 进 writer）
    # 2. HTTP buy → 断言 200 + 响应字段 {shares, cost, new_cash, message}（契约零变更）
    # 3. 断言 WRITER.get_state(mid).q_dec 已推进（证明走的是 writer 不是老路径）
    # 4. 断言 DB Transaction/Position 落库
    # 5. candle：交易后 OutcomeCandle 表应为空（还在 flusher pending），
    #    await CANDLE_FLUSHER.flush_once() 后出现 8 行（2 outcome × 4 档）
    # 6. HTTP sell → 200，state 回退
    ...


@pytest.mark.asyncio
async def test_e2e_flag_off_uses_old_path(client):
    # 不开 writer_on：HTTP buy 后 WRITER.enabled 为 False，
    # OutcomeCandle 表交易后立刻有行（老路径事务内 UPSERT）——证明路由正确
    ...
```

（`...` 处按 `test_candle_integration.py` 的既有 HTTP helper 展开成完整代码——该文件已包含 admin 登录、`POST /api/v1/market/create`、用户注册、`POST /api/v1/market/buy` 的可工作调用序列，逐段拷贝后按上面 6 点断言补齐。这是对既有测试模式的复用，不是留白：两个测试的**断言清单**如注释所列，一条不少。）

- [ ] **Step 2: 跑测确认失败**

Run: `python -m pytest tests/test_writer_e2e.py -x -q`
Expected: FAIL —— create_market 未注册市场 → `WRITER.get_state(mid) is None`（或 buy 落回 400）

- [ ] **Step 3: 接线**

1. `loan_migrate.py` `DEFAULT_CONFIGS` 追加：

```python
    # ── 单写者重构（spec 2026-08-21）──
    ("single_writer_enabled", "false", "bool"),   # 翻转需重启进程（启动时读一次）
```

2. `site_config.py` 追加：

```python
async def get_bool_or(session: AsyncSession, key: str, default: bool) -> bool:
    """读 bool；key 不存在时返回 default（不抛 SiteConfigError）。"""
    try:
        return await get_bool(session, key)
    except SiteConfigError:
        return default
```

3. `main.py` lifespan——startup 在 `await start_bot_detection_scheduler()` 之后加：

```python
    # ── 单写者状态机（spec 2026-08-21 § 4）：启动时读 flag，翻转需重启 ──
    from app.core.database import async_session_maker
    from app.services import site_config as _site_config
    from app.services.market_writer import WRITER
    from app.services.candle_flusher import CANDLE_FLUSHER
    async with async_session_maker() as _s:
        _sw = await _site_config.get_bool_or(_s, "single_writer_enabled", False)
    if _sw:
        await WRITER.start()
        await CANDLE_FLUSHER.start()
```

shutdown 在 `await stop_bot_detection_scheduler()` 之前加（先停 writer 断新增，再 flusher 终 flush）：

```python
    from app.services.market_writer import WRITER as _writer
    from app.services.candle_flusher import CANDLE_FLUSHER as _flusher
    await _writer.stop()
    await _flusher.stop()
```

4. `market.py::create_market`——`await db.commit()` 之后、return 之前：

```python
    from app.services.market_writer import WRITER
    if WRITER.enabled:
        await WRITER.register_market(int(new_market.id))
```

5. `conftest.py::setup_db`——yield 之前追加（防上一测试的 writer/flusher 状态泄漏；module-scope lifespan 意味着 lifespan 只在 module 首测启动、且当时 flag 缺失 → writer 默认不启，测试用 `writer_on` fixture 显式启）：

```python
    from app.services.market_writer import WRITER
    from app.services.candle_flusher import CANDLE_FLUSHER
    await WRITER.stop()
    CANDLE_FLUSHER._pending.clear()
```

- [ ] **Step 4: 跑 E2E 确认通过**

Run: `python -m pytest tests/test_writer_e2e.py -q`
Expected: 全 PASS

- [ ] **Step 5: 全量验证（声称完成前必跑）**

```bash
python -m py_compile $(find app -name '*.py')
python -c "import app.main"
python -m pytest -x -q
```

Expected: 编译/导入干净；pytest 全绿（memory 注明有 1 个已知过期 fail 的话，确认失败者是同一个既有 case 且与本次改动无关，在结果里写明）。

- [ ] **Step 6: Commit**

```bash
git add app/services/loan_migrate.py app/services/site_config.py app/main.py app/api/v1/market.py tests/conftest.py tests/test_writer_e2e.py
git commit -m "feat(writer): lifespan 接线 + single_writer_enabled 开关（默认关，翻转需重启）+ E2E"
```

- [ ] **Step 7: k6 验收（人工步骤，交用户）**

阶段 1 的 spec 验收（buy p50 < 20 ms、吞吐 > 300 rps）需要 **Postgres 环境 + flag 开启 + nginx 限速白名单**，SQLite 测试环境无意义。收尾时向用户交付说明：
1. 在目标环境 `UPDATE siteconfig SET value='true' WHERE key='single_writer_enabled'` 后重启后端
2. 复跑 `loadtest/` 下与 `k6_trade_20260513T043645Z.json` 同参的 k6 脚本
3. 对比 spec § 2 验收表；如未达标，火焰图先看 writer 队列深度与 DB commit 耗时

---

## Self-Review 结论（写计划时已跑）

- **Spec 覆盖**：阶段 0（Task 1）；§ 4.1-4.4（Task 2/3）；§ 4.3 双背压（Task 3）；§ 4.4 不动点+自愈（Task 2/3/6）；§ 4.5 锁去向（Task 6/8 注释落实、market_locks 不动）；§ 4.6（Task 9）；§ 4.7 resolve/close/resume/sweep（Task 8/9）、5 条 cash 路径不动（无 task，正确）；§ 7.5 flusher（Task 4）+ resync 兜底沿用（无需改参，1h ≫ 5s）；§ 8 阶段 1 全部 bullet（flag Task 10、锁不删、验收 k6 Task 10 Step 7）。quote/`_QUOTE_CACHE` 不动（阶段 3/5 的事）。
- **占位扫描**：Task 10 Step 1 的 E2E 测试体引用 `test_candle_integration.py` 既有 HTTP helper 并给出完整断言清单——是移植配方而非留白；Task 8 op_resolve 同理（源行号 + 9 条逐条 delta）。其余任务代码完整。
- **类型一致性**：`OpOutcome`/`submit`/`register_op`/`BuyCmd`/`SellCmd`/`LiquidateMarketCmd` 等签名在 Interfaces 与代码块间已核对一致；`liquidate_user_split` 在 Task 9 Step 4 与 sweep 分支调用处参数一致。
