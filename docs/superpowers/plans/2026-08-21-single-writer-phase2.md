# 单写者内存状态机 · 阶段 2 实施计划 —— 定频广播帧

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地 spec 阶段 2：8 Hz 定频 tick 帧（价格向量 + 帧内逐笔成交 + 状态并入）取代逐笔广播成为唯一市场数据帧；老 `trade` / `market_status` 事件由 `legacy_trade_events` 热开关双发兼容 quant bot；前端改吃 tick 帧；上线 build 版本自刷机制（阶段 3 前置）；quant bot 完成 tick 迁移。

**Architecture:** 新增 `TickBroadcaster` 单例：writer consumer 与老路径在每笔成交 / 状态变更 / 强平后 `feed_*`，全局 8 Hz loop 扫 dirty 市场把帧经 `BROKER.publish` 发出（沿用阶段 0 的序列化一次 bytes 通道）。**迁移期兼容不变式：tick 帧与老事件共用 BROKER 同一个 per-market seq 计数器**——quant bot 的解析器对未知事件类型同样参与 gap 检测（`quant/thccb_quant/client/sse.py:74-84`，未知 type 落入 else 分支查 seq 连续性），共用计数器让 bot 收到 tick 帧时 seq 连续、内容自然忽略，双发期间 bot 零改动不炸；随后 bot 增加 tick 适配器（帧→合成逐笔事件），策略层零改动。

**Tech Stack:** FastAPI + asyncio（后端）；Vue 3 + Pinia + EventSource（前端）；httpx SSE（quant bot）。无新运行时依赖。

**Spec:** `docs/superpowers/specs/2026-08-21-single-writer-design.md`（本计划实现其 § 5 全部、§ 8 阶段 2，并消化终审携带项 MIN-1 / MIN-5 / MIN-10 / MIN-11；终审报告见 `docs/superpowers/reviews/2026-08-21-single-writer-phase0-1-final-review.md`）

## Global Constraints

- 分支 `perf/2026-08-21-single-writer`，**不 push**（push 触发自动部署，完成后交用户决定）
- **迁移期 seq 语义 = 每事件 +1**（legacy 事件与 tick 帧共享同一 per-market 计数器）；`legacy_trade_events` 关掉后退化为 spec § 5.3 的"每帧 +1"。任何改动不得让 bot 或前端看到 seq 跳号
- `legacy_trade_events`：site_config 热开关，默认 `true`（60 s 缓存），阶段 5 连代码一起删
- tick 帧 `data` 形状（进 `sse_pack` 信封的 `data` 字段，信封 `type="tick"`，`seq`/`ts`/`market_id` 由 BROKER 填）：
  ```json
  {"status": "trading", "prices": [0.55, 0.45], "trades": [{…}, …], "settlement": {…}}
  ```
  `prices` 按 `outcome_ids` 升序、`float(quantize_price(p))` 8 位量化值（spec § 5.1 权威精度）；`trades` 内每笔 payload 形状与现有 legacy `trade` 事件的 `data.trade` **逐字段一致**（MIN-11 用测试锁死）；`settlement` 仅 settled 帧携带；强平/状态帧 `trades` 可为空数组；老路径（writer off）的纯状态帧 `prices` 可能为空数组（前端跳过 patch），writer on 恒有——文档化偏差
- **单进程 / 事件循环零阻塞 / 广播序列化一次**（tick 走 BROKER.publish 既有 bytes 通道，天然继承）
- 高敏感文件本计划会动：`realtime.py`、`market.py`、`stream.py`、前端 `stores/` `src/api/`——spec 已授权，改动最小化、每步测试兜底
- 红线文件 `.github/workflows/ci.yml` 本次**已获用户明确授权**（2026-08-21，仅 Task 5 的两处 build 注入行），除此之外不碰任何红线
- 后端验证（每 task commit 前）：`python -m py_compile $(find app -name '*.py')` + `python -c "import app.main"` + 该 task pytest；前端验证：`npm run type-check` + `npm run lint`；收尾全量 `python -m pytest -x -q`
- commit 风格：`feat:/fix:/refactor:/perf:/test:/docs:` + 中文；按文件 `git add <path>`
- 后端命令在 `backend/`、前端命令在 `thccb-frontend/`、bot 命令在 `quant/` 下执行

---

### Task 1: TickBroadcaster 模块 + legacy 双发开关

**Files:**
- Create: `backend/app/services/tick_broadcaster.py`
- Modify: `backend/app/services/loan_migrate.py`（配置种子表加一行）
- Test: `backend/tests/test_tick_broadcaster.py`

**Interfaces:**
- Consumes: `app.services.realtime.BROKER`（publish bytes 通道）、`app.services.site_config.get_bool_or`、`app.models.base.MarketStatus`
- Produces（后续 task 依赖的确切签名）:
  - `TICK_BROADCASTER: TickBroadcaster`（模块级单例）
  - `TICK_BROADCASTER.feed_trade(market_id: int, prices: list[float], trade: dict, status) -> None`
  - `TICK_BROADCASTER.feed_prices(market_id: int, prices: list[float], status) -> None`（强平：价格动、无成交）
  - `TICK_BROADCASTER.feed_status(market_id: int, status, settlement: dict | None = None, prices: list[float] | None = None) -> None`
  - `await TICK_BROADCASTER.flush_once() -> int`（发出的帧数；无 dirty 返回 0）
  - `await TICK_BROADCASTER.start()` / `await TICK_BROADCASTER.stop()`（stop 做最后一次 flush）
  - `await legacy_events_enabled() -> bool`（模块级函数）

- [ ] **Step 1: 写失败测试**

`backend/tests/test_tick_broadcaster.py`：

```python
"""TickBroadcaster 单元测试：帧形状 / 合并 / 双发开关种子。"""
import json

import pytest
from sqlmodel import SQLModel

from app.core.database import engine, async_session_maker
from app.services.realtime import BROKER
from app.services.tick_broadcaster import TICK_BROADCASTER, legacy_events_enabled


def _parse_sse(blob: bytes) -> dict:
    text = blob.decode("utf-8")
    assert text.endswith("\n\n")
    data_line = next(l for l in text.split("\n") if l.startswith("data: "))
    return json.loads(data_line[len("data: "):])


@pytest.fixture(autouse=True)
def _reset_broadcaster():
    TICK_BROADCASTER._pending.clear()
    yield


def _trade(i=1):
    return {"id": i, "type": "buy", "outcome_id": 11, "username": "alice",
            "shares": 1.0, "price": 0.5, "gross": 0.5, "fee": 0.0,
            "post_market_price": 0.51, "market_prices_post": [0.51, 0.49],
            "timestamp": "2026-08-21T00:00:00+00:00"}


@pytest.mark.asyncio
async def test_flush_sends_one_frame_and_drains():
    sub, _ = await BROKER.subscribe(7001)
    try:
        TICK_BROADCASTER.feed_trade(7001, [0.51, 0.49], _trade(1), "trading")
        TICK_BROADCASTER.feed_trade(7001, [0.52, 0.48], _trade(2), "trading")
        assert await TICK_BROADCASTER.flush_once() == 1
        payload = _parse_sse(sub.q.get_nowait())
        assert payload["type"] == "tick"
        assert payload["market_id"] == 7001
        assert payload["data"]["status"] == "trading"
        assert payload["data"]["prices"] == [0.52, 0.48]      # 帧价格取最后一笔
        assert [t["id"] for t in payload["data"]["trades"]] == [1, 2]  # 逐笔不丢
        assert "settlement" not in payload["data"]
        # 再 flush 必须 no-op（dirty 已清）
        assert await TICK_BROADCASTER.flush_once() == 0
        assert sub.q.empty()
    finally:
        await BROKER.unsubscribe(7001, sub)


@pytest.mark.asyncio
async def test_price_only_frame_has_empty_trades():
    sub, _ = await BROKER.subscribe(7002)
    try:
        TICK_BROADCASTER.feed_prices(7002, [0.6, 0.4], "trading")
        assert await TICK_BROADCASTER.flush_once() == 1
        payload = _parse_sse(sub.q.get_nowait())
        assert payload["data"]["trades"] == []
        assert payload["data"]["prices"] == [0.6, 0.4]
    finally:
        await BROKER.unsubscribe(7002, sub)


@pytest.mark.asyncio
async def test_settlement_carried_once():
    from app.models.base import MarketStatus
    sub, _ = await BROKER.subscribe(7003)
    try:
        TICK_BROADCASTER.feed_status(
            7003, MarketStatus.SETTLED,
            settlement={"winning_outcome_id": 11, "settled_at": "2026-08-21T01:00:00+00:00"},
            prices=[1.0, 0.0],
        )
        await TICK_BROADCASTER.flush_once()
        payload = _parse_sse(sub.q.get_nowait())
        assert payload["data"]["status"] == "settled"          # 枚举归一成小写值
        assert payload["data"]["settlement"]["winning_outcome_id"] == 11
        # settled 之后又来一笔 price feed → 下一帧不再带 settlement
        TICK_BROADCASTER.feed_prices(7003, [1.0, 0.0], "settled")
        await TICK_BROADCASTER.flush_once()
        payload2 = _parse_sse(sub.q.get_nowait())
        assert "settlement" not in payload2["data"]
    finally:
        await BROKER.unsubscribe(7003, sub)


@pytest.mark.asyncio
async def test_tick_shares_seq_counter_with_legacy_events():
    """迁移期不变式：legacy 事件与 tick 帧共用同一 per-market seq 计数器。"""
    sub, anchor = await BROKER.subscribe(7004)
    try:
        await BROKER.publish(7004, "trade", {"trade": _trade(9)})
        TICK_BROADCASTER.feed_trade(7004, [0.5, 0.5], _trade(9), "trading")
        await TICK_BROADCASTER.flush_once()
        legacy = _parse_sse(sub.q.get_nowait())
        tick = _parse_sse(sub.q.get_nowait())
        assert legacy["seq"] == anchor + 1
        assert tick["seq"] == anchor + 2       # 连续，无跳号
    finally:
        await BROKER.unsubscribe(7004, sub)


@pytest.mark.asyncio
async def test_legacy_flag_seeded_default_true():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
        await conn.run_sync(SQLModel.metadata.create_all)
    from app.services.loan_migrate import auto_migrate
    from app.services import site_config
    site_config.clear_cache()
    await auto_migrate()
    assert await legacy_events_enabled() is True
    async with async_session_maker() as s:
        assert await site_config.get_str(s, "legacy_trade_events") == "true"
```

- [ ] **Step 2: 跑测确认失败**

Run: `python -m pytest tests/test_tick_broadcaster.py -x -q`
Expected: FAIL —— `ModuleNotFoundError: app.services.tick_broadcaster`

- [ ] **Step 3: 实现 tick_broadcaster.py**

```python
"""定频广播帧 broadcaster（spec § 5.1）。

writer 新路径与 market.py 老路径在每笔成交/状态变更/强平后 feed 本模块；
全局 8 Hz tick loop 扫 dirty 市场，把「价格向量 + 帧窗口内逐笔成交 + 市场
状态」打成一个 tick 帧经 BROKER.publish 发出——publish 已是序列化一次投
bytes（阶段 0），tick 帧天然继承。

迁移期兼容不变式（本文件最重要的约束）：tick 帧与老 trade/market_status
事件共用 BROKER 的同一个 per-market seq 计数器。quant bot 的 SSE 解析器对
未知事件类型也参与 gap 检测（quant/.../sse.py:74），共用计数器让 bot 收到
tick 帧时 seq 仍连续、内容被忽略——双发期间 bot 零改动不受影响。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from app.core.database import async_session_maker
from app.models.base import MarketStatus
from app.services import site_config

logger = logging.getLogger(__name__)


async def legacy_events_enabled() -> bool:
    """老 trade/market_status 事件双发开关（spec § 5.4，阶段 5 连代码一起删）。

    site_config 60 s 进程缓存：缓存命中时 async_session_maker() 上下文不执行
    任何 SQL、不 checkout 连接，可在每次 publish 前调用。
    """
    async with async_session_maker() as s:
        return await site_config.get_bool_or(s, "legacy_trade_events", True)


def _status_str(status: Any) -> str:
    """帧内 status 统一为 "trading"/"halt"/"settled" 小写值。

    writer 状态可能是 MarketStatus 枚举（op_close 等赋 new_status），也可能是
    DB 读回的裸 str；str(MarketStatus.X) 会得到 "MarketStatus.X"，必须取 .value。
    """
    if isinstance(status, MarketStatus):
        return status.value
    return str(status)


@dataclass
class _Pending:
    prices: list[float] = field(default_factory=list)
    status: str = MarketStatus.TRADING.value
    trades: list[dict] = field(default_factory=list)
    settlement: Optional[dict] = None
    dirty: bool = False


class TickBroadcaster:
    TICK_INTERVAL = 0.125   # 8 Hz（spec § 5.1）

    def __init__(self) -> None:
        self._pending: dict[int, _Pending] = {}
        self._task: asyncio.Task | None = None

    def _entry(self, market_id: int) -> _Pending:
        return self._pending.setdefault(int(market_id), _Pending())

    def feed_trade(self, market_id: int, prices: list[float], trade: dict, status: Any) -> None:
        p = self._entry(market_id)
        p.prices = list(prices)
        p.status = _status_str(status)
        p.trades.append(trade)
        p.dirty = True

    def feed_prices(self, market_id: int, prices: list[float], status: Any) -> None:
        p = self._entry(market_id)
        p.prices = list(prices)
        p.status = _status_str(status)
        p.dirty = True

    def feed_status(self, market_id: int, status: Any,
                    settlement: Optional[dict] = None,
                    prices: Optional[list[float]] = None) -> None:
        p = self._entry(market_id)
        p.status = _status_str(status)
        if settlement is not None:
            p.settlement = settlement
        if prices is not None:
            p.prices = list(prices)
        p.dirty = True

    async def flush_once(self) -> int:
        from app.services.realtime import BROKER   # 局部 import 避免环
        sent = 0
        for market_id, p in list(self._pending.items()):
            if not p.dirty:
                continue
            data: dict = {
                "status": p.status,
                "prices": list(p.prices),
                "trades": p.trades,
            }
            if p.settlement is not None:
                data["settlement"] = p.settlement
            # 先摘 pending 再 await publish：publish 期间新 feed 进下一帧
            p.trades = []
            p.settlement = None
            p.dirty = False
            await BROKER.publish(market_id, "tick", data)
            sent += 1
        return sent

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="tick-broadcaster")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        try:
            await self.flush_once()   # 停机前把残帧发出去
        except Exception:
            logger.exception("tick broadcaster final flush failed")
        self._pending.clear()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.TICK_INTERVAL)
            try:
                await self.flush_once()
            except Exception:
                logger.exception("tick broadcaster loop error")


TICK_BROADCASTER = TickBroadcaster()
```

- [ ] **Step 4: loan_migrate.py 种子表加一行**

在 `("single_writer_enabled", "false", "bool"),` 那一行（`loan_migrate.py:53`）之后加：

```python
    ("legacy_trade_events", "true", "bool"),      # 阶段 2 双发老 SSE 事件；bot 迁移完关掉，阶段 5 删
```

- [ ] **Step 5: 跑测确认通过**

Run: `python -m pytest tests/test_tick_broadcaster.py -q && python -m py_compile $(find app -name '*.py') && python -c "import app.main"`
Expected: 全 PASS

- [ ] **Step 6: Commit**

```bash
git add app/services/tick_broadcaster.py app/services/loan_migrate.py tests/test_tick_broadcaster.py
git commit -m "feat(sse): TickBroadcaster 8Hz 定频帧——与老事件共用 seq 计数器，legacy_trade_events 双发开关（阶段 2）"
```

---

### Task 2: writer 接线 —— consumer 投喂 tick + MIN-1 价格不动点 + MIN-5 审计日志

**Files:**
- Modify: `backend/app/services/market_writer.py`（`OpOutcome` 字段、`_consume` apply 段、`MarketState.seq` 删除）
- Modify: `backend/app/services/writer_ops.py`（各 op 设置 `tick_trade` / `tick_settlement`、强平审计日志）
- Modify: `backend/app/services/liquidation_service.py`（`liquidate_user_mode` 日志补回，MIN-5）
- Test: `backend/tests/test_writer_tick.py`

**Interfaces:**
- Consumes: Task 1 的 `TICK_BROADCASTER` / `legacy_events_enabled`（**约定：market_writer 用 `from app.services import tick_broadcaster as _tick` 模块引用并调 `_tick.legacy_events_enabled()` / `_tick.TICK_BROADCASTER`，让测试可以 monkeypatch `app.services.tick_broadcaster.legacy_events_enabled`**）
- Produces:
  - `OpOutcome` 字段变更：**删** `new_prices`；**加** `tick_trade: Optional[dict] = None`、`tick_settlement: Optional[dict] = None`
  - `MarketState` **删** `seq` 字段（MIN-10：帧序号由 BROKER 统一计数，per-market state 不再自带——对 spec § 4.1 的实现层微调，Task 9 在 spec 补注）
  - consumer apply 后不变式：`st.prices == calculate_lmsr_with_prices(st.q, st.b)[1]`（MIN-1）

- [ ] **Step 1: 写失败测试**

`backend/tests/test_writer_tick.py`（`_fresh_db` / `_seed_market` / `_seed_user` fixture 从 `tests/test_writer_buy.py` 逐字拷贝——测试文件间不共享 helper；`_parse_sse` 从 Task 1 测试拷贝）：

```python
"""writer → tick 帧接线测试：形状一致 / 双发开关 / 价格不动点 / 强平空帧。"""
import json
from decimal import Decimal

import pytest

from app.services.lmsr import calculate_lmsr_with_prices, quantize_price
from app.services.market_writer import WRITER
from app.services.realtime import BROKER
from app.services.tick_broadcaster import TICK_BROADCASTER
from app.services.writer_ops import BuyCmd, CloseCmd, LiquidateMarketCmd, ResolveCmd

# （粘贴 _fresh_db / _seed_market / _seed_user / _buy / _parse_sse）

TRADE_KEYS = {"id", "type", "outcome_id", "username", "shares", "price", "gross",
              "fee", "post_market_price", "market_prices_post", "timestamp"}


@pytest.fixture(autouse=True)
def _reset_tick():
    TICK_BROADCASTER._pending.clear()
    yield


async def _drain_frames(sub):
    await TICK_BROADCASTER.flush_once()
    out = []
    while not sub.q.empty():
        out.append(_parse_sse(sub.q.get_nowait()))
    return out


@pytest.mark.asyncio
async def test_buy_emits_tick_frame_and_legacy_event_with_identical_trade_payload():
    """双发默认开：一笔 buy → 1 条 legacy trade + 1 帧 tick，trade payload 逐字段相同（MIN-11）。"""
    mid, oids = await _seed_market(shares=("0", "0"))
    uid = await _seed_user()
    await WRITER.start()
    sub, _ = await BROKER.subscribe(mid)
    try:
        await WRITER.submit(_buy(mid, oids[0], uid, shares="10", accept_any_slippage=True))
        payloads = await _drain_frames(sub)
        legacy = [p for p in payloads if p["type"] == "trade"]
        ticks = [p for p in payloads if p["type"] == "tick"]
        assert len(legacy) == 1 and len(ticks) == 1
        legacy_trade = legacy[0]["data"]["trade"]
        tick_trade = ticks[0]["data"]["trades"][0]
        assert set(legacy_trade.keys()) == TRADE_KEYS
        assert tick_trade == legacy_trade                       # 同一 payload 对象双投
        # seq 连续（共用计数器）
        seqs = sorted(p["seq"] for p in payloads)
        assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))
        # 帧价格 = writer state 的 8dp 量化
        st = WRITER.get_state(mid)
        assert ticks[0]["data"]["prices"] == [float(quantize_price(p)) for p in st.prices]
        assert ticks[0]["data"]["status"] == "trading"
    finally:
        await BROKER.unsubscribe(mid, sub)


@pytest.mark.asyncio
async def test_legacy_off_only_tick(monkeypatch):
    mid, oids = await _seed_market(shares=("0", "0"))
    uid = await _seed_user()
    await WRITER.start()

    async def _off():
        return False
    monkeypatch.setattr("app.services.tick_broadcaster.legacy_events_enabled", _off)

    sub, _ = await BROKER.subscribe(mid)
    try:
        await WRITER.submit(_buy(mid, oids[0], uid, shares="5", accept_any_slippage=True))
        payloads = await _drain_frames(sub)
        assert [p["type"] for p in payloads] == ["tick"]
    finally:
        await BROKER.unsubscribe(mid, sub)


@pytest.mark.asyncio
async def test_prices_fixpoint_after_buy():
    """MIN-1：apply 后 st.prices 恒等于由量化后 q 重新导出的价格。"""
    mid, oids = await _seed_market(shares=("3.5", "0"))
    uid = await _seed_user()
    await WRITER.start()
    await WRITER.submit(_buy(mid, oids[0], uid, shares="7", accept_any_slippage=True))
    st = WRITER.get_state(mid)
    _, derived = calculate_lmsr_with_prices(st.q, st.b)
    assert st.prices == derived        # 列表逐元素严格相等，不是近似


@pytest.mark.asyncio
async def test_close_frame_carries_status_and_resolve_carries_settlement():
    mid, oids = await _seed_market(shares=("2", "0"))
    await WRITER.start()
    sub, _ = await BROKER.subscribe(mid)
    try:
        await WRITER.submit(CloseCmd(market_id=mid))
        frames = [p for p in await _drain_frames(sub) if p["type"] == "tick"]
        assert frames[-1]["data"]["status"] == "halt"

        from app.services.writer_ops import ResumeCmd
        await WRITER.submit(ResumeCmd(market_id=mid))
        await _drain_frames(sub)

        await WRITER.submit(ResolveCmd(market_id=mid, winning_outcome_id=oids[0],
                                       payout=Decimal("1"), admin_id=1))
        frames = [p for p in await _drain_frames(sub) if p["type"] == "tick"]
        settled = frames[-1]["data"]
        assert settled["status"] == "settled"
        assert settled["settlement"]["winning_outcome_id"] == oids[0]
        assert "settled_at" in settled["settlement"]
    finally:
        await BROKER.unsubscribe(mid, sub)


@pytest.mark.asyncio
async def test_liquidation_emits_price_frame_with_empty_trades():
    """强平改价但无成交事件 → 空 trades 帧把新价格推出去（改进现状：老架构强平不发 SSE）。"""
    mid, oids = await _seed_market(shares=("0", "0"))
    uid = await _seed_user(cash="1000")
    await WRITER.start()
    await WRITER.submit(_buy(mid, oids[0], uid, shares="20", accept_any_slippage=True))
    sub, _ = await BROKER.subscribe(mid)
    try:
        await TICK_BROADCASTER.flush_once()     # 清掉 buy 的残帧（sub 在 buy 后才订阅，收不到）
        res = await WRITER.submit(LiquidateMarketCmd(
            market_id=mid, user_id=uid, mode="emergency", partial_pct=Decimal("1")))
        assert res["sold_count"] == 1
        frames = [p for p in await _drain_frames(sub) if p["type"] == "tick"]
        assert len(frames) == 1
        assert frames[0]["data"]["trades"] == []
        st = WRITER.get_state(mid)
        assert frames[0]["data"]["prices"] == [float(quantize_price(p)) for p in st.prices]
    finally:
        await BROKER.unsubscribe(mid, sub)
```

- [ ] **Step 2: 跑测确认失败**

Run: `python -m pytest tests/test_writer_tick.py -x -q`
Expected: FAIL —— tick 帧不存在（只收到 legacy 事件）

- [ ] **Step 3: 改 market_writer.py**

1. `MarketState` 删 `seq: int = 0` 字段与注释（MIN-10；帧序号由 BROKER 计数）。全仓 `grep -rn "\.seq" app/services/market_writer.py app/services/writer_ops.py` 确认无引用。
2. `OpOutcome`：删 `new_prices: Optional[list[float]] = None`，加：

```python
    tick_trade: Optional[dict] = None        # 本笔成交 payload（与 legacy trade 事件同一 dict）
    tick_settlement: Optional[dict] = None   # 仅 resolve：{"winning_outcome_id", "settled_at"}
```

3. 顶部加 `from app.services.lmsr import calculate_lmsr_with_prices, quantize_cost, quantize_price` 与 `from app.services import tick_broadcaster as _tick`。
4. `_consume` 的 apply 段整体替换为：

```python
                outcome: OpOutcome = await op(st, cmd)
                # ── commit 已成功（op 返回即视为已 commit）→ apply 内存（spec § 4.4）──
                if outcome.new_q_dec is not None:
                    st.q_dec = outcome.new_q_dec
                    st.q = [float(x) for x in st.q_dec]
                    # MIN-1：prices 恒从量化后的 q 重新导出，保证 prices == f(q)。
                    # 阶段 2 起 prices 进 tick 帧，若沿用 op 浮点直加算出的 new_prices，
                    # 重启/自愈从镜像重读后帧价格会出现末位跳变
                    _, st.prices = calculate_lmsr_with_prices(st.q, st.b)
                if outcome.new_status is not None:
                    st.status = outcome.new_status
                if outcome.candle_rows:
                    self._merge_candles(outcome.candle_rows)
                # ── tick 帧投喂（spec § 5.1）──
                prices_8dp = [float(quantize_price(p)) for p in st.prices]
                if outcome.tick_trade is not None:
                    _tick.TICK_BROADCASTER.feed_trade(
                        market_id, prices_8dp, outcome.tick_trade, st.status)
                elif outcome.new_status is not None:
                    _tick.TICK_BROADCASTER.feed_status(
                        market_id, st.status, outcome.tick_settlement, prices=prices_8dp)
                elif outcome.new_q_dec is not None:
                    # 强平：q 变了但没有成交事件 → 空 trades 帧推新价格
                    _tick.TICK_BROADCASTER.feed_prices(market_id, prices_8dp, st.status)
                # ── 老事件双发（legacy_trade_events；阶段 5 删）──
                if outcome.publishes and await _tick.legacy_events_enabled():
                    for event_type, data in outcome.publishes:
                        await BROKER.publish(market_id, event_type, data)
                if not fut.done():
                    fut.set_result(outcome.response)
```

（`from app.services.realtime import BROKER` 的局部 import 保留在 `_consume` 顶部。）

- [ ] **Step 4: 改 writer_ops.py**

1. `op_buy`：SSE payload dict 提为变量并双投（`publishes` 与 `tick_trade` 共享同一 dict——MIN-11 的"单一构造点"）：

```python
    trade_payload = {
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
    }
    return OpOutcome(
        response={...同现状...},
        new_q_dec=new_q_dec,
        candle_rows=candle_rows,
        tick_trade=trade_payload,
        publishes=[("trade", {"trade": trade_payload})],
    )
```

（删掉 `new_prices=new_prices` 入参——`OpOutcome` 已无此字段；`new_prices` 局部变量保留给 candle/响应计算。）

2. `op_sell` 同样改造（payload 提变量、`fee`/`gross` 字段照旧）。
3. `op_close` / `op_resume`：`publishes` 不变，无 `tick_*` 字段（consumer 依据 `new_status` feed_status）。
4. `op_resolve`：加

```python
        tick_settlement={"winning_outcome_id": winning_id,
                         "settled_at": settled_at.isoformat()},
```

5. `op_liquidate_market`（MIN-5 上半）：非 TRADING 早返回处加审计日志（与老路径同事件名，先 `grep -n "liquidation_skip_non_trading_market" app/services/liquidation_service.py` 对齐字段）：

```python
    if state.status != MarketStatus.TRADING:
        logger.warning(
            "liquidation_skip_non_trading_market(writer) user_id=%s market_id=%s status=%s",
            cmd.user_id, cmd.market_id, state.status,
        )
        return OpOutcome(response={"sold_count": 0, "total_proceeds": ZERO})
```

- [ ] **Step 5: liquidation_service.py 补 `liquidate_user_mode` 日志（MIN-5 下半）**

先 `grep -n "liquidate_user_mode" app/services/liquidation_service.py` 找老路径原始日志的字段集；在 `liquidate_user_split` 阶段 A（user 锁内 margin 复检通过之后、进入阶段 B 之前）补同名日志：

```python
    logger.info(
        "liquidate_user_mode(writer) user_id=%s mode=%s partial_pct=%s pre_margin_ratio=%s hard=%s",
        user_id, mode, partial_pct, pre_margin_ratio, hard_threshold,
    )
```

（变量名以该函数实际签名为准——执行时打开文件对照，字段集合必须 ⊇ 老路径日志的字段。）

- [ ] **Step 6: 跑测确认通过**

Run: `python -m pytest tests/test_writer_tick.py tests/test_writer_buy.py tests/test_writer_sell.py tests/test_writer_admin_ops.py tests/test_writer_liquidation.py tests/test_market_writer_loop.py -q`
Expected: 全 PASS（既有 writer 测试若断言了 `OpOutcome.new_prices` 或 `MarketState.seq`，同步更新——grep 确认）

- [ ] **Step 7: Commit**

```bash
git add app/services/market_writer.py app/services/writer_ops.py app/services/liquidation_service.py tests/test_writer_tick.py
git commit -m "feat(writer): consumer 投喂 tick 帧+legacy 双发门控——prices 恒从 q 导出(MIN-1)，强平审计日志补回(MIN-5)"
```

---

### Task 3: 老路径接线 —— market.py 五处 publish 改双发

**Files:**
- Modify: `backend/app/api/v1/market.py`（buy `:656-674`、sell `:829-847`、close `:253-257`、resume `:1160-1164`、resolve `:1003-1011`）
- Test: `backend/tests/test_market_tick_oldpath.py`

**Interfaces:**
- Consumes: Task 1 的 `_tick.TICK_BROADCASTER` / `site_config.get_bool_or`（老路径 handler 手里有 db session，直接用它读开关，不走 `legacy_events_enabled()` 的新 session）
- Produces: flag off（writer 未启用）时 tick 帧照样产生——阶段 2 上线后无论 writer 开关如何，前端都吃得到帧

- [ ] **Step 1: 写失败测试**

`backend/tests/test_market_tick_oldpath.py`（用与 `tests/test_candle_integration.py` 相同的 client fixture 模式发 HTTP buy；`_parse_sse` / `TRADE_KEYS` 拷贝自 Task 2 测试）：

```python
"""老路径（writer off）→ tick 帧接线：HTTP buy 触发帧 + 双发开关。"""
import pytest

from app.services.realtime import BROKER
from app.services.tick_broadcaster import TICK_BROADCASTER

# （拷贝本仓库既有 HTTP 集成测试的 client/登录/建市场 fixture——
#   打开 tests/test_candle_integration.py 照抄其 setup 结构）

TRADE_KEYS = {"id", "type", "outcome_id", "username", "shares", "price", "gross",
              "fee", "post_market_price", "market_prices_post", "timestamp"}


@pytest.mark.asyncio
async def test_oldpath_buy_emits_tick_and_legacy(client, seeded_market):
    market_id, outcome_id = seeded_market
    sub, _ = await BROKER.subscribe(market_id)
    try:
        r = await client.post("/api/v1/market/buy",
                              json={"outcome_id": outcome_id, "shares": 5,
                                    "accept_any_slippage": True},
                              headers=auth_headers)
        assert r.status_code == 200
        await TICK_BROADCASTER.flush_once()
        payloads = []
        while not sub.q.empty():
            payloads.append(_parse_sse(sub.q.get_nowait()))
        legacy = [p for p in payloads if p["type"] == "trade"]
        ticks = [p for p in payloads if p["type"] == "tick"]
        assert len(legacy) == 1 and len(ticks) == 1
        assert ticks[0]["data"]["trades"][0] == legacy[0]["data"]["trade"]
        assert set(ticks[0]["data"]["trades"][0].keys()) == TRADE_KEYS   # 与 writer 路径同形状（MIN-11）
        assert ticks[0]["data"]["status"] == "trading"
        assert len(ticks[0]["data"]["prices"]) == 2
    finally:
        await BROKER.unsubscribe(market_id, sub)


@pytest.mark.asyncio
async def test_oldpath_legacy_off(client, seeded_market, monkeypatch):
    """开关关掉：老事件不再发，tick 帧照发。"""
    market_id, outcome_id = seeded_market
    from app.services import site_config
    async def _false(session, key, default):
        if key == "legacy_trade_events":
            return False
        return default
    monkeypatch.setattr("app.api.v1.market.site_config.get_bool_or", _false)
    sub, _ = await BROKER.subscribe(market_id)
    try:
        r = await client.post("/api/v1/market/buy", json={...同上...}, headers=auth_headers)
        assert r.status_code == 200
        await TICK_BROADCASTER.flush_once()
        payloads = [...]
        assert [p["type"] for p in payloads] == ["tick"]
    finally:
        await BROKER.unsubscribe(market_id, sub)


@pytest.mark.asyncio
async def test_oldpath_close_and_resolve_frames(client, seeded_market):
    """close → status=halt 帧；resolve → settlement 帧（老路径管理端点）。"""
    # POST /{id}/close → flush → 帧 status == "halt"，帧内 market_status legacy 事件同现
    # POST /{id}/resume → POST /{id}/resolve → flush → settlement.winning_outcome_id 断言
    ...按上面两个测试的样式写全，断言与 Task 2 的 close/resolve 测试对应...
```

（`...` 处执行时按同文件前两个测试的完整样式补全——不是留白，是照抄结构改断言；写完后本文件不允许存在 `...`。）

- [ ] **Step 2: 跑测确认失败**

Run: `python -m pytest tests/test_market_tick_oldpath.py -x -q`
Expected: FAIL —— 只有 legacy 事件，无 tick 帧

- [ ] **Step 3: 改 market.py**

顶部 import 区加 `from app.services import tick_broadcaster as _tick`。

1. **buy**（`:653-674` publish 块）——payload 提变量、feed、门控：

```python
    # SSE payload 包含足够字段让前端增量更新 marketTrades + 价格，无需 refetch
    # market_prices_post: 全市场所有 outcome 的 post 价快照（按 outcome.id 升序）
    trade_payload = {
        "id": int(tx.id),
        "type": TransactionType.BUY,
        "outcome_id": int(outcome.id),
        "username": user.username,
        "shares": float(shares_d),
        "price": float(avg_price),
        "gross": float(pay),
        "fee": 0.0,
        "post_market_price": float(post_mp),
        "market_prices_post": [float(p) for p in new_prices],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _tick.TICK_BROADCASTER.feed_trade(
        int(market.id),
        [float(quantize_price(p)) for p in new_prices],
        trade_payload,
        market.status,
    )
    if await site_config.get_bool_or(db, "legacy_trade_events", True):
        await BROKER.publish(market.id, "trade", {"trade": trade_payload})
```

2. **sell**（`:826-847`）同构改造（`fee`/`gross` 用 sell 的值）。
3. **close**（`:253-257`）：

```python
    _tick.TICK_BROADCASTER.feed_status(market_id, MarketStatus.HALT)
    if await site_config.get_bool_or(db, "legacy_trade_events", True):
        await BROKER.publish(market_id, "market_status", {"status": MarketStatus.HALT})
```

4. **resume**（`:1160-1164`）同构，status `MarketStatus.TRADING`。
5. **resolve**（`:1003-1011`）：

```python
    _tick.TICK_BROADCASTER.feed_status(
        market.id, MarketStatus.SETTLED,
        settlement={"winning_outcome_id": int(winning.id), "settled_at": now.isoformat()},
    )
    if await site_config.get_bool_or(db, "legacy_trade_events", True):
        await BROKER.publish(market.id, "market_status", {...原 payload 不动...})
```

（老路径状态帧不带 prices——Global Constraints 已记录该文档化偏差，前端对空 `prices` 跳过 patch。）

- [ ] **Step 4: 跑测确认通过**

Run: `python -m pytest tests/test_market_tick_oldpath.py tests/test_candle_integration.py tests/test_market_slippage_lock.py -q`
Expected: 全 PASS（既有集成测试若断言了「publish 恰好 N 次」需检查是否受 tick 帧影响——tick 帧只在显式 flush 时发出，正常不影响）

- [ ] **Step 5: Commit**

```bash
git add app/api/v1/market.py tests/test_market_tick_oldpath.py
git commit -m "feat(sse): 老路径五处 publish 接 tick 帧+legacy 门控——两路径 trade payload 单一形状(MIN-11)"
```

---

### Task 4: lifespan 接线 + QUEUE_MAXSIZE 32

**Files:**
- Modify: `backend/app/main.py`（lifespan 启停）
- Modify: `backend/app/services/realtime.py`（`QUEUE_MAXSIZE`）
- Test: 既有 `tests/test_realtime_broker.py` 全绿 + `tests/test_writer_e2e.py` 全绿

- [ ] **Step 1: main.py lifespan**

startup：在 `if _sw:` 块**之外**（无论 writer 是否启用，老路径也投喂）、`start_bot_detection_scheduler()` 之后加：

```python
    # ── 定频广播帧（spec § 5.1）：writer 与老路径共用，无条件启动 ──
    from app.services.tick_broadcaster import TICK_BROADCASTER
    await TICK_BROADCASTER.start()
```

shutdown：顺序改为 调度器 → writer → **broadcaster（最后 flush 残帧）** → flusher：

```python
    await stop_bot_detection_scheduler()
    await stop_liquidation_scheduler()
    await stop_loan_scheduler()
    from app.services.market_writer import WRITER as _writer
    from app.services.candle_flusher import CANDLE_FLUSHER as _flusher
    from app.services.tick_broadcaster import TICK_BROADCASTER as _tick_b
    await _writer.stop()
    await _tick_b.stop()      # writer 已停无新 feed；最后一次 flush 把残帧发给订阅者
    await _flusher.stop()
    await engine.dispose()
```

（更新该处既有顺序注释，补一句 broadcaster 的位置理由。）

- [ ] **Step 2: realtime.py QUEUE_MAXSIZE**

```python
    MAX_SUBSCRIBERS_PER_MARKET = 500
    # 定频帧后队列深度 == "落后几帧"：32 帧 ≈ 4 s 落后容忍（spec § 5.2）。
    # 迁移期（legacy_trade_events 开）队列里混有老事件，踢出判定比 4 s 更严格；
    # 阶段 5 关双发后回归纯帧语义。慢消费者踢出 + kicked 机制原样保留。
    QUEUE_MAXSIZE = 32
```

- [ ] **Step 3: 跑测确认**

Run: `python -m pytest tests/test_realtime_broker.py tests/test_writer_e2e.py -q && python -c "import app.main"`
Expected: 全 PASS。若 `test_realtime_broker.py` 有测试依赖大队列（grep `QUEUE_MAXSIZE`——现有慢消费者测试自建小队列 Subscriber，预期不受影响），按新值修正断言而非调回 2000。

- [ ] **Step 4: Commit**

```bash
git add app/main.py app/services/realtime.py
git commit -m "feat(sse): broadcaster lifespan 接线（停机最后 flush）+ 队列深度 2000→32 帧语义（阶段 2）"
```

---

### Task 5: build 版本注入 —— snapshot 携带前端 build hash

**Files:**
- Modify: `backend/Dockerfile`（ARG/ENV 两行）
- Modify: `.github/workflows/ci.yml`（**红线文件，用户已于 2026-08-21 明确授权本任务的两处改动，不得顺手改其他内容**）
- Modify: `backend/app/api/v1/stream.py`（snapshot 加字段）
- Test: `backend/tests/test_stream_build_hash.py`

**背景**：CI 里前后端从同一 commit 构建（`github.sha`）。后端镜像把 sha 烧进 `APP_BUILD_SHA` 环境变量、snapshot 下发；前端构建把同一 sha 烧进 `import.meta.env.VITE_BUILD_SHA`；两者不一致 = 旧 tab 在跑旧代码 → 提示刷新（阶段 3 砍 summary 字段的前置防线，spec § 8 阶段 2）。部署窗口内（前端 rsync 先于后端 up 完成）会出现短暂 sha 不一致——因此前端只**提示**不强刷（Task 6）。

- [ ] **Step 1: Dockerfile**

`EXPOSE 8004` 之前加：

```dockerfile
# 前端 build 版本自刷机制（spec § 8 阶段 2）：CI 把 github.sha 烧进环境，
# snapshot 下发给前端比对。本地构建默认 "dev"（前端侧空值/dev 不触发提示）。
ARG BUILD_SHA=dev
ENV APP_BUILD_SHA=$BUILD_SHA
```

- [ ] **Step 2: ci.yml 两处（仅此两处）**

后端 `Build and push Docker image` 步骤的 `with:` 里加：

```yaml
          build-args: |
            BUILD_SHA=${{ github.sha }}
```

前端 `Build` 步骤的 `env:` 里加：

```yaml
          VITE_BUILD_SHA: ${{ github.sha }}
```

- [ ] **Step 3: stream.py**

顶部 `import os`；`_build_snapshot` 返回 dict 加一行（放 `"outcomes"` 之前）：

```python
        # build 版本自刷机制：前端比对自己的 VITE_BUILD_SHA，不一致提示刷新（阶段 2）
        "frontend_build": os.environ.get("APP_BUILD_SHA", ""),
```

- [ ] **Step 4: 写测试并跑过**

`backend/tests/test_stream_build_hash.py`：

```python
import pytest

from app.api.v1.stream import _build_snapshot

# （拷贝 test_writer_buy.py 的 _fresh_db / _seed_market fixture）


@pytest.mark.asyncio
async def test_snapshot_carries_build_sha(monkeypatch):
    monkeypatch.setenv("APP_BUILD_SHA", "abc123")
    mid, _ = await _seed_market()
    from app.core.database import async_session_maker
    async with async_session_maker() as db:
        snap = await _build_snapshot(db, mid)
    assert snap["frontend_build"] == "abc123"


@pytest.mark.asyncio
async def test_snapshot_build_sha_defaults_empty(monkeypatch):
    monkeypatch.delenv("APP_BUILD_SHA", raising=False)
    mid, _ = await _seed_market()
    from app.core.database import async_session_maker
    async with async_session_maker() as db:
        snap = await _build_snapshot(db, mid)
    assert snap["frontend_build"] == ""
```

Run: `python -m pytest tests/test_stream_build_hash.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/Dockerfile .github/workflows/ci.yml backend/app/api/v1/stream.py backend/tests/test_stream_build_hash.py
git commit -m "feat(deploy): snapshot 携带前端 build hash——CI 注入 github.sha，旧 tab 自刷前置（阶段 2，ci.yml 改动已获授权）"
```

---

### Task 6: 前端 tick 基建 —— 类型 / 流层 / useMarketRealtime / build 横幅

**Files:**
- Modify: `thccb-frontend/src/types/stream.ts`
- Modify: `thccb-frontend/src/api/stream.ts`
- Modify: `thccb-frontend/src/composables/useMarketRealtime.ts`
- Create: `thccb-frontend/src/composables/useBuildVersion.ts`
- Modify: `thccb-frontend/src/components/layout/AppHeader.vue`（teleport 横幅）
- Modify: `thccb-frontend/src/env.d.ts` 或 `vite-env.d.ts`（先 `ls src/*.d.ts` 找到现有声明文件，补 `VITE_BUILD_SHA`）

**Interfaces（Produces，Task 7 依赖）:**
- `useMarketRealtime` 返回值新增：`latestTick: Ref<TickFrameData | null>`、`outcomesOrder: Ref<number[]>`、`tickSeen: Ref<boolean>`
- 语义：收到首个 tick 帧后 `tickSeen=true`，此后 legacy `trade`/`market_status` 只参与 seq 连续性、不再更新任何状态（防双发双应用）；tick 帧 status 变化时更新既有 `latestMarketStatus`（TradingView 现有 watcher 零改动复用）
- `useBuildVersion.ts`: `export const buildMismatch: Ref<boolean>` + `export function reportServerBuild(sha: string | undefined): void`

- [ ] **Step 1: types/stream.ts**

```ts
export interface MarketEvent {
  type: 'snapshot' | 'trade' | 'market_status' | 'ping' | 'tick'
  market_id: number
  ts: string
  data: any
  seq?: number
}

// 8 Hz 定频帧（阶段 2）：价格向量 + 帧窗口内逐笔成交 + 市场状态。
// 迁移期与老 trade/market_status 事件共用同一 seq 计数器（每事件 +1）；
// legacy_trade_events 关掉后退化为"每帧 +1"。
export interface TickFrameData {
  status: 'trading' | 'halt' | 'settled'
  /** 全 outcome 当前价，按 outcome.id 升序，8 位小数（服务端权威精度） */
  prices: number[]
  /** 帧窗口内逐笔成交，形状与老 trade 事件的 data.trade 逐字段一致；可为空数组 */
  trades: TradeEventData['trade'][]
  /** 仅 settled 帧携带 */
  settlement?: { winning_outcome_id: number; settled_at: string }
}
```

- [ ] **Step 2: api/stream.ts**

- constructor 里 `this.listeners.set('tick', new Set())`
- `isValidMarketEvent` 的类型数组加 `'tick'`
- `setupEventHandlers` 加 `this.eventSource.addEventListener('tick', handleNamedEvent('tick'))`

- [ ] **Step 3: useBuildVersion.ts**

```ts
import { ref } from 'vue'

/** 模块级单例：任何页面的 SSE snapshot 报告的服务端 build 与本地不一致时置 true。
 *  只提示不强刷——部署窗口内前端 rsync 先于后端重启完成，短暂 sha 不一致是正常态。 */
export const buildMismatch = ref(false)

export function reportServerBuild(sha: string | undefined): void {
  const mine = import.meta.env.VITE_BUILD_SHA
  if (!sha || !mine || sha === 'dev' || mine === 'dev') return
  if (sha !== mine) buildMismatch.value = true
}
```

- [ ] **Step 4: useMarketRealtime.ts**

改动点（保持既有代码风格与注释密度）：

1. 返回接口与 state 增加：

```ts
  const latestTick = ref<TickFrameData | null>(null)
  const outcomesOrderRef = ref<number[]>([])
  const tickSeen = ref(false)
  let lastFrameStatus: string | null = null
```

2. `handleSnapshot`：`outcomesOrder` 赋值处同步 `outcomesOrderRef.value = outcomesOrder`；`lastFrameStatus = snap.status ?? null`；末尾加 `reportServerBuild((evt.data as any).frontend_build)`（顶部 import）。
3. 新增 `handleTick`：

```ts
  const handleTick = (evt: MarketEvent) => {
    tickSeen.value = true
    checkInlineGap(evt)
    const frame = evt.data as TickFrameData

    // 价格向量全量 patch（帧价格是服务端 8dp 权威值；空数组 = 老路径纯状态帧，跳过）
    if (frame.prices.length && frame.prices.length === outcomesOrder.length) {
      const next = new Map<number, number>()
      for (let i = 0; i < outcomesOrder.length; i++) {
        next.set(outcomesOrder[i]!, frame.prices[i]!)
      }
      pricesByOutcome.value = next
    }

    // 状态变更并入帧（spec § 5.1）：变化时喂给既有 latestMarketStatus 消费方
    if (frame.status !== lastFrameStatus) {
      lastFrameStatus = frame.status
      latestMarketStatus.value = {
        status: frame.status,
        ...(frame.settlement ?? {}),
      } as MarketStatusEventData
    }

    latestTick.value = frame
  }
```

4. `handleTrade` / `handleMarketStatus` 改为双发防重（**gap 检测必须继续计数**——迁移期 legacy 事件与 tick 共用计数器）：

```ts
  const handleTrade = (evt: MarketEvent) => {
    checkInlineGap(evt)
    if (tickSeen.value) return   // tick 帧已接管状态更新；老事件只参与 seq 连续性（双发防重）
    ...原有全部应用逻辑不动...
  }
```

（`handleMarketStatus` 同样在 `checkInlineGap` 后加 `if (tickSeen.value) return`。）

5. `stream.on('tick', handleTick)` 注册 + `onBeforeUnmount` 对称 `off`；`watch(marketId)` 的切市场清理块加 `latestTick.value = null; tickSeen.value = false; lastFrameStatus = null; outcomesOrderRef.value = []`。
6. 返回对象加 `latestTick, outcomesOrder: outcomesOrderRef, tickSeen`；`UseMarketRealtimeReturn` 接口同步。

- [ ] **Step 5: AppHeader.vue 横幅**

script 加：

```ts
import { buildMismatch } from '@/composables/useBuildVersion'
const refreshPage = () => location.reload()
```

template 根部（现有根 div 之后，Vue 3 多根）加：

```html
  <Teleport to="body">
    <div v-if="buildMismatch" class="build-refresh-bar">
      <span>站点已更新——当前页面运行的是旧版本，继续操作可能出错</span>
      <button type="button" class="build-refresh-btn" @click="refreshPage">立即刷新</button>
    </div>
  </Teleport>
```

style（工业风：黑底白字、无圆角、粗边框）：

```css
.build-refresh-bar {
  position: fixed;
  top: 0; left: 0; right: 0;
  z-index: 9999;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 16px;
  padding: 10px 16px;
  background: #000;
  color: #fff;
  border-bottom: 3px solid #f5a623;
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.04em;
}
.build-refresh-btn {
  padding: 4px 14px;
  border: 2px solid #fff;
  background: #fff;
  color: #000;
  font-size: 12px;
  font-weight: 800;
  cursor: pointer;
}
.build-refresh-btn:hover { background: #f5a623; border-color: #f5a623; }
```

（已知局限，写进代码注释：检查只在建立 SSE（进交易页 / 断线重连 / 切后台回来）时发生；停留在 Portfolio 等无 SSE 页面的旧 tab 拿不到提示——阶段 3 的 NaN 兜底主要保护的就是交易页，可接受。）

- [ ] **Step 6: 验证**

Run: `npm run type-check && npm run lint`
Expected: 0 error（env.d.ts 里 `VITE_BUILD_SHA?: string` 声明补上后 type-check 才会过）

- [ ] **Step 7: Commit**

```bash
git add src/types/stream.ts src/api/stream.ts src/composables/useMarketRealtime.ts src/composables/useBuildVersion.ts src/components/layout/AppHeader.vue src/env.d.ts
git commit -m "feat(sse): 前端 tick 帧基建——useMarketRealtime 吃帧+双发防重+build 版本横幅（阶段 2）"
```

（`src/env.d.ts` 路径按 Step 1 实际找到的声明文件调整。）

---

### Task 7: 前端消费方切换 —— TradingView / 图表吃 tick 帧

**Files:**
- Modify: `thccb-frontend/src/pages/market/TradingView.vue`
- Modify: `thccb-frontend/src/components/chart/PriceChart.vue`
- Modify: `thccb-frontend/src/components/chart/CandleChart.vue`

**Interfaces:**
- Consumes: Task 6 的 `latestTick` / `outcomesOrder` / `tickSeen`
- 既有 `latestTrade` watcher **全部保留不删**：`tickSeen` 后 `latestTrade` 不再更新，watcher 自然沉默；它是「新前端 + 老后端」部署窗口的降级路径（此窗口内无 tick 帧，legacy 事件仍驱动 UI）

- [ ] **Step 1: TradingView.vue**

在既有 `watch(realtime.latestTrade, ...)` 之后加（不动原 watcher）：

```ts
// ── tick 帧驱动的本地状态更新（阶段 2）──
// 一帧可含多笔成交，逐笔 append；帧价格向量整体 patch（服务端 8dp 权威值）。
// 老 latestTrade watcher 保留作为「新前端+老后端」部署窗口的降级路径：
// tickSeen 后 latestTrade 不再更新，它自然沉默。
watch(realtime.latestTick, (frame) => {
  if (!frame) return
  if (marketStore.tradeLoading) return  // 自己刚下单尚未完成，避免冲突
  for (const t of frame.trades) {
    marketStore.appendTradeFromSSE(t)
  }
  if (frame.prices.length) {
    marketStore.patchAllPricesFromTrade(frame.prices)   // 帧价格与 market_prices_post 同序（id 升序）
  }
  if (frame.trades.length) maybeSanityRefresh()
})
```

（`latestMarketStatus` watcher 零改动——Task 6 已让 tick 帧的状态变化流进它。）

- [ ] **Step 2: PriceChart.vue**

`if (realtime)` 块内，在既有 `watch(realtime.latestTrade, ...)` 之后加：

```ts
  // tick 帧：一帧多笔逐笔 append（用每笔的 market_prices_post 取本 outcome 的成交后价，
  // 保证帧内多笔的中间点不丢；latestTrade watcher 在 tick 模式下自然沉默，见 TradingView 注释）
  watch(realtime.latestTick, (frame) => {
    if (!frame) return
    const order = realtime.outcomesOrder.value
    const idx = order.indexOf(props.outcomeId)
    for (const t of frame.trades) {
      const price = idx >= 0 && t.market_prices_post?.length === order.length
        ? t.market_prices_post[idx]!
        : (t.outcome_id === props.outcomeId ? t.post_market_price : undefined)
      if (price === undefined) continue
      appendPoint(price, new Date(t.timestamp).getTime())
    }
  })
```

- [ ] **Step 3: CandleChart.vue**

同位置加：

```ts
  watch(realtime.latestTick, (frame) => {
    if (!frame) return
    const order = realtime.outcomesOrder.value
    const idx = order.indexOf(props.outcomeId)
    for (const t of frame.trades) {
      const price = idx >= 0 && t.market_prices_post?.length === order.length
        ? t.market_prices_post[idx]!
        : (t.outcome_id === props.outcomeId ? t.post_market_price : undefined)
      if (price === undefined) continue
      const isDirectTrade = t.outcome_id === props.outcomeId
      applyTrade(price, isDirectTrade ? t.shares : 0, new Date(t.timestamp).getTime(), isDirectTrade)
    }
  })
```

- [ ] **Step 4: 验证 + 浏览器实测**

Run: `npm run type-check && npm run lint`
Expected: 0 error

浏览器实测（起本地前后端；起不来则在日志写「未实测 UI」，不得谎称通过）：
- 交易页开两个 tab，一边下单——另一边 125 ms 内价格/最近成交/图表更新（tick 帧）
- 关掉 `legacy_trade_events`（admin site_config 或 SQL）→ 行为不变（帧独立驱动）
- 快速连续下 3 单 → 最近成交逐笔出现、K 线 volume 正确累计（帧内多笔不丢）
- admin close/resume/settle → 状态横幅/结算 UI 正常（status 并入帧）
- 断网 5 s 重连 → gap reconcile 正常（seq 连续性）
- 移动端宽度过一遍主路径

- [ ] **Step 5: Commit**

```bash
git add src/pages/market/TradingView.vue src/components/chart/PriceChart.vue src/components/chart/CandleChart.vue
git commit -m "feat(sse): TradingView/图表改吃 tick 帧——帧内逐笔 append，legacy watcher 留作部署窗口降级（阶段 2）"
```

---

### Task 8: quant bot tick 适配器 + 契约文档

**Files:**
- Modify: `quant/thccb_quant/client/sse.py`（仅 `SseEvent.type` Literal 加 `"tick"`——gap 检测逻辑**一行不动**，未知类型本来就走 else 分支参与 seq 连续性）
- Modify: `quant/thccb_quant/client/sse_subscriber.py`（tick 适配器）
- Modify: `quant/docs/sse-contract.md`
- Test: `quant/tests/test_sse_subscriber_tick.py`（新增；先 `ls quant/tests/` 对齐既有测试的 fixture 风格）

**设计**：适配器把 tick 帧翻译成合成的逐笔 `trade` / `market_status` 事件喂给既有 dispatch——**策略层零改动**。双发期间帧内成交与 legacy 事件重复，靠既有 `store.log_trade` 的 trade_id 去重；帧来源的重复**静默跳过**（不进 `_dedup_skipped_count`，那个计数器的告警语义是"异常重复"，双发期的帧内重复是设计内的常态，用独立计数器）。

- [ ] **Step 1: 写失败测试**

`quant/tests/test_sse_subscriber_tick.py`（fake store / fake strategy，参考既有 `quant/tests/` 里对 SseSubscriber 的测试写法；若无既有 SseSubscriber 测试则用如下自足结构）：

```python
"""tick 帧适配器：帧→合成逐笔事件，策略零改动。"""
import asyncio

import pytest

from thccb_quant.client.sse import SseEvent
from thccb_quant.client.sse_subscriber import SseSubscriber


class FakeStore:
    def __init__(self):
        self.logged: list[dict] = []
        self.known_ids: set[int] = set()

    async def log_trade(self, *, market_id, payload):
        tid = int(payload["trade"]["id"])
        if tid in self.known_ids:
            return False
        self.known_ids.add(tid)
        self.logged.append(payload)
        return True

    async def bulk_insert_partial_trades(self, items):
        return 0


class FakeStrategy:
    name = "fake"
    market_id = 1

    def __init__(self):
        self.events: list[SseEvent] = []

    async def on_sse_event(self, event):
        self.events.append(event)


def _subscriber(store, strat):
    import structlog
    return SseSubscriber(rest=None, store=store, sse_client=None,
                         strategies=[strat], market_ids=[1],
                         logger=structlog.get_logger("test"))


def _tick(seq, trades, status="trading", settlement=None):
    data = {"status": status, "prices": [0.5, 0.5], "trades": trades}
    if settlement:
        data["settlement"] = settlement
    return SseEvent(type="tick", seq=seq, data=data)


def _t(i):
    return {"id": i, "type": "buy", "outcome_id": 1, "username": "u",
            "shares": 1.0, "price": 0.5, "gross": 0.5, "fee": 0.0,
            "post_market_price": 0.5, "market_prices_post": [0.5, 0.5],
            "timestamp": "2026-08-21T00:00:00+00:00"}


@pytest.mark.asyncio
async def test_tick_trades_dispatched_as_synthetic_trade_events():
    store, strat = FakeStore(), FakeStrategy()
    sub = _subscriber(store, strat)
    await sub._handle_event(1, _tick(5, [_t(1), _t(2)]))
    assert [e.type for e in strat.events] == ["trade", "trade"]
    assert [e.data["trade"]["id"] for e in strat.events] == [1, 2]
    assert len(store.logged) == 2


@pytest.mark.asyncio
async def test_tick_dedups_against_legacy_events_silently():
    """双发期：legacy 事件先到已入库 → 帧内同 id 静默跳过，不派发第二次。"""
    store, strat = FakeStore(), FakeStrategy()
    sub = _subscriber(store, strat)
    await sub._handle_event(1, SseEvent(type="trade", seq=4, data={"trade": _t(1)}))
    await sub._handle_event(1, _tick(5, [_t(1)]))
    assert [e.data["trade"]["id"] for e in strat.events if e.type == "trade"] == [1]
    assert sub.dedup_skipped_count == 0            # 帧内重复不污染异常告警计数器
    assert sub.tick_dedup_count == 1


@pytest.mark.asyncio
async def test_tick_status_change_emits_synthetic_market_status_once():
    store, strat = FakeStore(), FakeStrategy()
    sub = _subscriber(store, strat)
    await sub._handle_event(1, SseEvent(type="snapshot", seq=1,
                                        data={"status": "trading", "outcomes": []}))
    await sub._handle_event(1, _tick(2, []))                       # 状态没变 → 不合成
    await sub._handle_event(1, _tick(3, [], status="halt"))        # 变了 → 合成一次
    await sub._handle_event(1, _tick(4, [], status="halt"))        # 没再变 → 不再合成
    ms = [e for e in strat.events if e.type == "market_status"]
    assert len(ms) == 1
    assert ms[0].data["status"] == "halt"


@pytest.mark.asyncio
async def test_tick_settled_carries_settlement_fields():
    store, strat = FakeStore(), FakeStrategy()
    sub = _subscriber(store, strat)
    await sub._handle_event(1, SseEvent(type="snapshot", seq=1,
                                        data={"status": "trading", "outcomes": []}))
    await sub._handle_event(1, _tick(2, [], status="settled",
                                     settlement={"winning_outcome_id": 9,
                                                 "settled_at": "2026-08-21T01:00:00+00:00"}))
    ms = [e for e in strat.events if e.type == "market_status"]
    assert ms[0].data["winning_outcome_id"] == 9
```

- [ ] **Step 2: 跑测确认失败**

Run（quant 目录，用它自己的 venv——先 `ls quant/.venv/bin/ 2>/dev/null` 确认，有则 `quant/.venv/bin/python -m pytest`，无则按 `quant/README` 的方式）: `python -m pytest tests/test_sse_subscriber_tick.py -x -q`
Expected: FAIL —— tick 分支不存在（事件被无声吞掉）

- [ ] **Step 3: 实现**

`sse.py`：`SseEvent.type` 的 Literal 加 `"tick"`（一行）。

`sse_subscriber.py`：

1. `__init__` 加 `self._last_status: dict[int, str] = {}` 与 `self._tick_dedup_count = 0`；property：

```python
    @property
    def tick_dedup_count(self) -> int:
        """帧内成交与 legacy 事件重复被跳过的次数（双发期设计内常态，与
        dedup_skipped_count 的异常告警语义分开）。"""
        return self._tick_dedup_count
```

2. `_handle_event` 的 `elif event.type == "snapshot":` 分支里加 `self._last_status[market_id] = str(event.data.get("status", ""))`（在既有日志行之后）。
3. `_handle_event` 加分支（放在 `snapshot` 分支之前均可，风格与既有 if/elif 链一致）：

```python
            elif event.type == "tick":
                await self._handle_tick(market_id, event)
```

4. 新方法：

```python
    async def _handle_tick(self, market_id: int, event: SseEvent) -> None:
        """tick 帧适配器（主站阶段 2）：帧 → 合成逐笔 trade / market_status 事件，
        喂给既有 dispatch，策略层零改动。双发期与 legacy 事件的重复靠
        log_trade 的 trade_id 去重，静默跳过。"""
        frame = event.data or {}
        for t in frame.get("trades", []):
            payload = {"trade": t}
            is_new = await self._store.log_trade(market_id=market_id, payload=payload)
            if not is_new:
                self._tick_dedup_count += 1
                continue
            await self._dispatch(market_id, SseEvent(type="trade", seq=event.seq, data=payload))
        status = frame.get("status")
        prev = self._last_status.get(market_id)
        if status and status != prev:
            self._last_status[market_id] = status
            if prev:   # snapshot 未到过（prev 为空）不合成，等 bootstrap
                data = {"status": status}
                if frame.get("settlement"):
                    data.update(frame["settlement"])
                await self._dispatch(market_id, SseEvent(type="market_status",
                                                         seq=event.seq, data=data))
```

- [ ] **Step 4: 更新 sse-contract.md**

在「事件类型」一节加 `tick` 小节（帧形状、prices 8dp 权威精度与升序契约、trades 逐笔与 legacy trade 同形状、settlement、"迁移期与老事件共用 seq 计数器（每事件 +1），`legacy_trade_events` 关闭后退化为每帧 +1"）；`snapshot` 一节补 `frontend_build` 字段（bot 忽略）；文末加迁移说明：bot 已内建 tick 适配，legacy 关闭不影响 bot；`market_status` / 逐笔 `trade` 事件标注"迁移期保留，主站阶段 5 移除"。

- [ ] **Step 5: 跑测确认通过 + bot 全量测试**

Run: `python -m pytest tests/ -q`（quant 目录）
Expected: 全 PASS

- [ ] **Step 6: Commit**

```bash
git add thccb_quant/client/sse.py thccb_quant/client/sse_subscriber.py docs/sse-contract.md tests/test_sse_subscriber_tick.py
git commit -m "feat(quant): SSE tick 帧适配器——帧转合成逐笔事件，策略零改动，双发去重独立计数"
```

---

### Task 9: 收尾 —— spec 补注 + 全量验证

**Files:**
- Modify: `docs/superpowers/specs/2026-08-21-single-writer-design.md`

- [ ] **Step 1: spec 补注（三处，各一小段）**

1. § 4.1 `MarketState.seq` 字段处补注：*实现时取消——帧序号由 BROKER 的 per-market 计数器统一分配（见 § 5.3 补注），state 不自带 seq。*
2. § 5.3 末尾补一段：*迁移期语义：tick 帧与 legacy `trade`/`market_status` 事件共用同一 per-market seq 计数器，seq = 每事件 +1；`legacy_trade_events` 关闭后只剩 tick 帧，自然退化为"每帧 +1"。选择共用计数器的原因：quant bot 的解析器对未知事件类型也做 seq 连续性检查，独立计数器会让未迁移的 bot 持续误判 gap 重连。*
3. § 5.4 末尾补：*bot 已于阶段 2 内建 tick 适配器（帧→合成逐笔事件，策略零改动），关闭双发开关不再受 bot 迁移进度约束；关开关的时机由用户在生产观察后决定。*

- [ ] **Step 2: 三端全量验证**

```bash
cd backend && python -m py_compile $(find app -name '*.py') && python -c "import app.main" && python -m pytest -x -q
cd ../thccb-frontend && npm run type-check && npm run lint
cd ../quant && python -m pytest tests/ -q
```

Expected: 全 PASS（后端约 12 s 全量、1 个既知过期 fail 之外全绿——对照 `MEMORY.md` 的 pytest 注意事项）

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-08-21-single-writer-design.md
git commit -m "docs(spec): 阶段 2 实现补注——共用 seq 计数器的迁移期语义与 bot 兼容论证"
```

- [ ] **Step 4: 交付说明（对用户，一句话格式）**

改了什么 / 分支 / 验证结果 / 未决风险（k6 重跑与 `legacy_trade_events` 关闭时机留给用户在生产决定；nginx 无改动）。

---

## Self-Review 记录（写计划时已核）

- spec § 5.1（帧形状/8dp/状态并入/无变更不发帧）→ Task 1/2/3；§ 5.2（32 帧队列）→ Task 4；§ 5.3（seq 语义 + 前端逻辑不变）→ Task 1 共用计数器设计 + Task 6；§ 5.4（双发 + 开关 + bot + 契约文档）→ Task 1/3/8；§ 8 阶段 2 的 build 自刷 → Task 5/6；携带项 MIN-1/5/10/11 → Task 2/3。
- 已知偏差（均已文档化）：MarketState.seq 取消（Task 9 spec 补注）；老路径纯状态帧 prices 可为空（Global Constraints + 前端跳过 patch）；强平发空 trades 帧（改进项，Task 2 测试锁行为）。
- 类型/签名一致性：`TickFrameData` ↔ 后端帧 dict、`feed_*` 签名在 Task 1 定义后 Task 2/3 逐字引用、`latestTick/outcomesOrder/tickSeen` 在 Task 6 产出 Task 7 消费。
