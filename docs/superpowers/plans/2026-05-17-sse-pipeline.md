# SSE Pipeline Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 SSE 数据管道：订阅 enabled 策略的 market、持久化 trade 事件到 SQLite、dispatch 给 `Strategy.on_sse_event` hook。

**Architecture:** SseClient 单 market 长连接（snapshot bootstrap + seq gap detection 重连）；SseSubscriber 协调多 market + 写 `trades` 表 + dispatch；启动时通过 REST `recent-trades` 预加载 100 条历史到 `partial_trades` 表。

**Tech Stack:** httpx async streaming / aiosqlite / structlog / respx (test mock)

**Spec:** `docs/superpowers/specs/2026-05-17-sse-pipeline-design.md`

---

## File Structure

| File | Op | Responsibility |
|---|---|---|
| `quant/thccb_quant/state/schema.sql` | modify | 加 `trades` + `partial_trades` 表 |
| `quant/thccb_quant/state/store.py` | modify | 加 `log_trade` / `bulk_insert_partial_trades` / `recent_trades_observed` |
| `quant/thccb_quant/client/rest.py` | modify | 加 `get_recent_trades` + `PartialTrade` 模型 |
| `quant/thccb_quant/strategy/base.py` | modify | Strategy ABC 加 `market_id: int \| None` 属性 |
| `quant/thccb_quant/strategy/dca.py` | modify | setup 内 `self.market_id = int(self._config["market_id"])` |
| `quant/thccb_quant/strategy/grid.py` | modify | setup 内同步 `self.market_id = self._market_id` |
| `quant/config.example.yaml` | modify | DCA 示例补 `market_id` 必填 |
| `quant/thccb_quant/client/sse.py` | rewrite | SseClient + SseEvent dataclass |
| `quant/thccb_quant/client/sse_subscriber.py` | create | SseSubscriber 协调 + dispatch + preload |
| `quant/thccb_quant/trader.py` | modify | 拆分 setup/tick；启动 SseSubscriber |
| `quant/tests/test_store_trades.py` | create | trades + partial_trades 表读写 |
| `quant/tests/test_rest_recent_trades.py` | create | get_recent_trades 解析 |
| `quant/tests/test_sse_client.py` | create | wire 解析 + gap + 重连 |
| `quant/tests/test_sse_subscriber.py` | create | dispatch + preload + 错误隔离 |
| `quant/tests/test_strategy_dca.py` | modify | 适配 market_id 字段 |
| `quant/tests/test_strategy_grid.py` | modify | 适配 market_id 属性 |

---

## Task 1: Schema + Store + RestClient.get_recent_trades

**Files:**
- Modify: `quant/thccb_quant/state/schema.sql`
- Modify: `quant/thccb_quant/state/store.py`
- Modify: `quant/thccb_quant/client/rest.py`
- Create: `quant/tests/test_store_trades.py`
- Create: `quant/tests/test_rest_recent_trades.py`

- [ ] **Step 1: 写测试 `quant/tests/test_store_trades.py`**

```python
from decimal import Decimal
from pathlib import Path
import pytest
from thccb_quant.state.store import Store


@pytest.fixture
async def store(tmp_path: Path):
    s = await Store.open(tmp_path / "t.db")
    yield s
    await s.close()


async def test_log_trade_inserts_full_row(store: Store):
    payload = {
        "trade": {
            "id": 1001, "type": "BUY", "outcome_id": 5,
            "username": "alice", "shares": 2.5, "price": 0.42,
            "gross": 1.05, "fee": 0.0, "post_market_price": 0.43,
            "market_prices_post": [0.43, 0.57], "timestamp": "2026-05-17T07:00:00Z",
        }
    }
    await store.log_trade(market_id=1, payload=payload)
    rows = await store.recent_trades_observed(market_id=1, limit=10)
    assert len(rows) == 1
    assert rows[0]["trade_id"] == 1001
    assert rows[0]["username"] == "alice"
    assert rows[0]["market_prices_post_json"] == "[0.43, 0.57]"


async def test_log_trade_dedup_by_trade_id(store: Store):
    payload = {"trade": {
        "id": 2001, "type": "BUY", "outcome_id": 1, "username": "x",
        "shares": 1.0, "price": 0.5, "gross": 0.5, "fee": 0.0,
        "post_market_price": 0.5, "market_prices_post": [0.5, 0.5],
        "timestamp": "2026-05-17T07:00:00Z",
    }}
    await store.log_trade(market_id=1, payload=payload)
    await store.log_trade(market_id=1, payload=payload)  # INSERT OR IGNORE
    rows = await store.recent_trades_observed(market_id=1, limit=10)
    assert len(rows) == 1


async def test_bulk_insert_partial_trades_idempotent(store: Store):
    items = [
        {"id": 100, "outcome_id": 1, "type": "BUY", "shares": "1.0",
         "price": "0.5", "username": "u1", "timestamp": "2026-05-17T07:00:00Z",
         "market_id": 1, "market_title": "M", "outcome_label": "yes"},
        {"id": 101, "outcome_id": 1, "type": "SELL", "shares": "2.0",
         "price": "0.6", "username": "u2", "timestamp": "2026-05-17T07:01:00Z",
         "market_id": 1, "market_title": "M", "outcome_label": "yes"},
    ]
    inserted = await store.bulk_insert_partial_trades(items)
    assert inserted == 2
    # 再插一遍 → 0 行新增
    inserted2 = await store.bulk_insert_partial_trades(items)
    assert inserted2 == 0
```

- [ ] **Step 2: 写测试 `quant/tests/test_rest_recent_trades.py`**

```python
import time
from decimal import Decimal
from pathlib import Path
import httpx
import pytest
import respx
from thccb_quant.client.auth import TokenManager
from thccb_quant.client.rest import RestClient


def _jwt(exp=3600):
    import base64, json
    p = {"exp": int(time.time()) + exp}
    return (
        base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
        + "." + base64.urlsafe_b64encode(json.dumps(p).encode()).rstrip(b"=").decode()
        + ".sig"
    )


@respx.mock
async def test_get_recent_trades_parses(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("")
    respx.get("http://x/api/v1/market/recent-trades").mock(
        return_value=httpx.Response(200, json=[
            {"id": 10, "outcome_id": 1, "type": "BUY", "shares": "2.5",
             "price": "0.42", "username": "alice",
             "timestamp": "2026-05-17T07:00:00Z",
             "market_id": 1, "market_title": "M", "outcome_label": "yes"},
        ])
    )
    async with httpx.AsyncClient(base_url="http://x") as raw, \
               httpx.AsyncClient(base_url="http://x") as cli:
        mgr = TokenManager(base_url="http://x", access_token=_jwt(),
                           refresh_token=_jwt(86400), env_path=env, raw_client=raw)
        rest = RestClient(client=cli, token_manager=mgr, rate_limit_per_sec=100)
        rs = await rest.get_recent_trades(limit=100)
    assert len(rs) == 1
    assert rs[0].id == 10
    assert rs[0].username == "alice"
    assert rs[0].market_id == 1
```

- [ ] **Step 3: 跑测试确认 FAIL**

```bash
cd /data/sunyunbo/www/TouhouCCB/quant && source .venv/bin/activate
pytest tests/test_store_trades.py tests/test_rest_recent_trades.py -v
```
Expected: AttributeError / not implemented errors on store + rest

- [ ] **Step 4: 改 `quant/thccb_quant/state/schema.sql`（追加到末尾）**

```sql

CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trade_id INTEGER NOT NULL UNIQUE,
  ts TEXT NOT NULL,
  ingest_ts TEXT NOT NULL,
  market_id INTEGER NOT NULL,
  outcome_id INTEGER NOT NULL,
  side TEXT NOT NULL,
  shares TEXT NOT NULL,
  price TEXT NOT NULL,
  gross TEXT NOT NULL,
  fee TEXT NOT NULL,
  username TEXT,
  post_market_price TEXT NOT NULL,
  market_prices_post_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_market_ts ON trades(market_id, ts);

CREATE TABLE IF NOT EXISTS partial_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trade_id INTEGER NOT NULL UNIQUE,
  ts TEXT NOT NULL,
  market_id INTEGER NOT NULL,
  outcome_id INTEGER NOT NULL,
  side TEXT NOT NULL,
  shares TEXT NOT NULL,
  price TEXT NOT NULL,
  username TEXT,
  market_title TEXT,
  outcome_label TEXT
);
CREATE INDEX IF NOT EXISTS idx_partial_trades_market_ts ON partial_trades(market_id, ts);
```

- [ ] **Step 5: 改 `quant/thccb_quant/state/store.py`（追加 3 个方法）**

在文件末尾追加（class Store 内）：

```python
    async def log_trade(self, *, market_id: int, payload: dict) -> None:
        """SSE trade event 入 trades 表。INSERT OR IGNORE 防重连重复。

        payload 形如 {"trade": {id, type, outcome_id, username, shares, price,
        gross, fee, post_market_price, market_prices_post, timestamp}}
        """
        import json as _json
        t = payload["trade"]
        await self._conn.execute(
            "INSERT OR IGNORE INTO trades "
            "(trade_id, ts, ingest_ts, market_id, outcome_id, side, shares, "
            " price, gross, fee, username, post_market_price, market_prices_post_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(t["id"]),
                str(t["timestamp"]),
                _utcnow_iso(),
                market_id,
                int(t["outcome_id"]),
                str(t["type"]),
                str(t["shares"]),
                str(t["price"]),
                str(t["gross"]),
                str(t["fee"]),
                t.get("username"),
                str(t["post_market_price"]),
                _json.dumps(t["market_prices_post"]),
            ),
        )
        await self._conn.commit()

    async def bulk_insert_partial_trades(self, items: list[dict]) -> int:
        """批量 INSERT OR IGNORE recent-trades 响应，返回实际插入条数。

        item 字段：id, outcome_id, type, shares, price, username, timestamp,
        market_id, market_title, outcome_label
        """
        if not items:
            return 0
        cur = await self._conn.executemany(
            "INSERT OR IGNORE INTO partial_trades "
            "(trade_id, ts, market_id, outcome_id, side, shares, price, "
            " username, market_title, outcome_label) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (int(i["id"]), str(i["timestamp"]), int(i["market_id"]),
                 int(i["outcome_id"]), str(i["type"]), str(i["shares"]),
                 str(i["price"]), i.get("username"),
                 i.get("market_title"), i.get("outcome_label"))
                for i in items
            ],
        )
        await self._conn.commit()
        return cur.rowcount

    async def recent_trades_observed(
        self, *, market_id: int | None = None, limit: int = 50
    ) -> list[dict]:
        if market_id is not None:
            cur = await self._conn.execute(
                "SELECT * FROM trades WHERE market_id = ? ORDER BY id DESC LIMIT ?",
                (market_id, limit),
            )
        else:
            cur = await self._conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
            )
        return [dict(r) for r in await cur.fetchall()]
```

- [ ] **Step 6: 改 `quant/thccb_quant/client/rest.py`（加 PartialTrade + 方法）**

在 `UserSummary` class 之后、`_TokenBucket` 之前追加：

```python
class PartialTrade(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    outcome_id: int
    type: str
    shares: Decimal
    price: Decimal
    username: Optional[str] = None
    timestamp: str
    market_id: int
    market_title: Optional[str] = None
    outcome_label: Optional[str] = None
```

在 `get_user_summary` 方法之后追加：

```python
    async def get_recent_trades(self, *, limit: int = 100) -> List[PartialTrade]:
        r = await self._request(
            "GET", "/api/v1/market/recent-trades",
            params={"limit": limit},
        )
        return [PartialTrade.model_validate(x) for x in r.json()]
```

- [ ] **Step 7: 跑测试 + 全套**

```bash
pytest tests/test_store_trades.py tests/test_rest_recent_trades.py -v
pytest 2>&1 | tail -3
```
Expected: 新增 4 个 test passed；全套从 44 → 48 passed

- [ ] **Step 8: Commit**

```bash
cd /data/sunyunbo/www/TouhouCCB
git add quant/thccb_quant/state/schema.sql quant/thccb_quant/state/store.py \
        quant/thccb_quant/client/rest.py \
        quant/tests/test_store_trades.py quant/tests/test_rest_recent_trades.py
git commit -m "feat(quant): trades/partial_trades 表 + Store/Rest 配套方法"
```

---

## Task 2: Strategy.market_id 属性 + DCA/Grid setup + config

**Files:**
- Modify: `quant/thccb_quant/strategy/base.py`
- Modify: `quant/thccb_quant/strategy/dca.py`
- Modify: `quant/thccb_quant/strategy/grid.py`
- Modify: `quant/config.example.yaml`
- Modify: `quant/tests/test_strategy_dca.py`
- Modify: `quant/tests/test_strategy_grid.py`

- [ ] **Step 1: 改 `quant/thccb_quant/strategy/base.py`，Strategy 类加属性**

在 `class Strategy(ABC):` 的属性区追加（在 `tick_interval_sec: int = 30` 后）：

```python
    market_id: int | None = None  # 由策略 setup() 设置；SseSubscriber 用以路由
```

- [ ] **Step 2: 改 `quant/thccb_quant/strategy/dca.py`，setup 设置 market_id**

在 `DcaStrategy.__init__` 末尾追加：

```python
        self._market_id: int = int(config["market_id"])
```

在 `DcaStrategy.setup` 方法第一行（`self._ctx = ctx` 之前）追加：

```python
        self.market_id = self._market_id
```

- [ ] **Step 3: 改 `quant/thccb_quant/strategy/grid.py`，setup 设置 market_id**

在 `GridStrategy.setup` 方法第一行（`self._ctx = ctx` 之前）追加：

```python
        self.market_id = self._market_id
```

- [ ] **Step 4: 改 `quant/config.example.yaml`，DCA 块加 market_id**

将：

```yaml
  - name: dca_market_1_outcome_yes
    type: dca
    enabled: false
    outcome_id: 1
    cny_per_buy: 5.0
    interval_hours: 6
    total_budget_cny: 200
```

替换为：

```yaml
  - name: dca_market_1_outcome_yes
    type: dca
    enabled: false
    market_id: 1
    outcome_id: 1
    cny_per_buy: 5.0
    interval_hours: 6
    total_budget_cny: 200
```

- [ ] **Step 5: 改 `quant/tests/test_strategy_dca.py`，cfg 加 market_id**

把所有 `cfg = {"outcome_id": 1, ...}` 字典都加 `"market_id": 1` 字段。三个 test 都有同样的 cfg dict，一并改。例：

```python
    cfg = {
        "market_id": 1,
        "outcome_id": 1, "cny_per_buy": 5.0,
        "interval_hours": 6, "total_budget_cny": 200,
    }
```

新增一个 test：

```python
async def test_dca_setup_sets_market_id(store: Store):
    cfg = {"market_id": 7, "outcome_id": 1, "cny_per_buy": 5.0,
           "interval_hours": 6, "total_budget_cny": 200}
    s = DcaStrategy(name="d", config=cfg)
    ctx = await _make_ctx(store)
    await s.setup(ctx)
    assert s.market_id == 7
```

- [ ] **Step 6: 改 `quant/tests/test_strategy_grid.py`，加 setup 测**

在文件末尾追加：

```python
async def test_grid_setup_sets_market_id(store: Store):
    s = GridStrategy(name="g", config=_cfg())
    ctx = await _make_ctx(store, current_price=0.40)
    await s.setup(ctx)
    assert s.market_id == 1
```

- [ ] **Step 7: 全套测试**

```bash
cd /data/sunyunbo/www/TouhouCCB/quant && source .venv/bin/activate
pytest 2>&1 | tail -3
```
Expected: 48 + 2 (新测) = 50 passed

- [ ] **Step 8: Commit**

```bash
cd /data/sunyunbo/www/TouhouCCB
git add quant/thccb_quant/strategy/base.py quant/thccb_quant/strategy/dca.py \
        quant/thccb_quant/strategy/grid.py quant/config.example.yaml \
        quant/tests/test_strategy_dca.py quant/tests/test_strategy_grid.py
git commit -m "feat(quant): Strategy.market_id 属性 + DCA config 加 market_id 必填"
```

---

## Task 3: SseClient 真实现

**Files:**
- Modify: `quant/thccb_quant/client/sse.py` (rewrite, replace skeleton)
- Create: `quant/tests/test_sse_client.py`

- [ ] **Step 1: 写测试 `quant/tests/test_sse_client.py`**

```python
"""SseClient: wire format 解析、gap detection、重连。

用 respx 不太方便 mock streaming，改用直接 mock httpx.AsyncClient.stream。
"""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from thccb_quant.client.auth import TokenManager
from thccb_quant.client.sse import SseClient, SseEvent


def _jwt(exp=3600):
    import base64, json
    p = {"exp": int(time.time()) + exp}
    return (
        base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
        + "." + base64.urlsafe_b64encode(json.dumps(p).encode()).rstrip(b"=").decode()
        + ".sig"
    )


class FakeStream:
    """模拟 httpx 流式响应。`lines` 是按顺序 yield 的文本行。"""

    def __init__(self, lines: list[str], status_code: int = 200):
        self._lines = lines
        self.status_code = status_code

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)


def _mk_client_with_streams(stream_factories):
    """stream_factories: 每次调 client.stream() 返回一个 FakeStream。

    iter 用完后再调会抛 StopIteration（用来终结测试循环）。
    """
    it = iter(stream_factories)
    client = MagicMock()

    @asynccontextmanager
    async def _stream(*args, **kwargs):
        try:
            s = next(it)
        except StopIteration:
            raise httpx.ConnectError("no more streams")
        yield s

    client.stream = _stream
    return client


async def _mk_sse(tmp_path: Path, http_client) -> SseClient:
    env = tmp_path / ".env"
    env.write_text("")
    raw = httpx.AsyncClient(base_url="http://x")
    mgr = TokenManager(base_url="http://x", access_token=_jwt(),
                       refresh_token=_jwt(86400), env_path=env, raw_client=raw)
    sse = SseClient(base_url="http://x", token_manager=mgr, raw_client=http_client)
    return sse


async def test_parses_snapshot_and_trade(tmp_path: Path):
    stream = FakeStream([
        "event: snapshot",
        'data: {"type":"snapshot","seq":0,"data":{"id":1,"outcomes":[]}}',
        "",
        "event: trade",
        'data: {"type":"trade","seq":1,"data":{"trade":{"id":99}}}',
        "",
    ])
    client = _mk_client_with_streams([stream])
    sse = await _mk_sse(tmp_path, client)
    got = []
    async def collect():
        async for ev in sse.subscribe(1):
            got.append(ev)
            if len(got) >= 2:
                return
    try:
        await asyncio.wait_for(collect(), timeout=2.0)
    except asyncio.TimeoutError:
        pass
    assert len(got) == 2
    assert got[0].type == "snapshot"
    assert got[0].seq == 0
    assert got[1].type == "trade"
    assert got[1].seq == 1
    assert got[1].data["trade"]["id"] == 99


async def test_gap_triggers_reconnect_rebootstrap(tmp_path: Path):
    """seq 跳跃 → 关连接重连，新 snapshot 重置 lastSeq。"""
    stream1 = FakeStream([
        "event: snapshot",
        'data: {"type":"snapshot","seq":10,"data":{}}',
        "",
        "event: trade",
        'data: {"type":"trade","seq":11,"data":{"trade":{"id":1}}}',
        "",
        "event: trade",
        'data: {"type":"trade","seq":15,"data":{"trade":{"id":2}}}',  # gap
        "",
    ])
    stream2 = FakeStream([
        "event: snapshot",
        'data: {"type":"snapshot","seq":20,"data":{}}',
        "",
    ])
    client = _mk_client_with_streams([stream1, stream2])
    sse = await _mk_sse(tmp_path, client)
    got = []
    async def collect():
        async for ev in sse.subscribe(1):
            got.append(ev)
            if len(got) >= 4:
                return
    try:
        await asyncio.wait_for(collect(), timeout=2.0)
    except asyncio.TimeoutError:
        pass
    # 期望：snap(10)、trade(11)、trade(15) 之后触发重连 → snap(20) 带 gap_recover
    types_seqs = [(ev.type, ev.seq) for ev in got]
    assert ("snapshot", 10) in types_seqs
    assert ("trade", 11) in types_seqs
    assert ("snapshot", 20) in types_seqs
    snap20 = next(ev for ev in got if ev.type == "snapshot" and ev.seq == 20)
    assert snap20.data.get("gap_recover") is True
```

- [ ] **Step 2: 跑测试确认 FAIL**

```bash
pytest tests/test_sse_client.py -v
```
Expected: 当前 sse.py 是 skeleton，构造就抛 NotImplementedError

- [ ] **Step 3: 重写 `quant/thccb_quant/client/sse.py`**

```python
"""SseClient: 长连接 SSE，单 market 一个实例。spec §5。

自动处理：wire 解析 / 25s 心跳超时 / 58 min 主动重连 / seq gap detection
→ 重连重 bootstrap / 网络错误指数退避 0.5/1/2/5/10s 封顶。
"""
import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Literal, Optional

import httpx
import structlog

from thccb_quant.client.auth import TokenManager

_log = structlog.get_logger("sse_client")

ZOMBIE_TIMEOUT_SEC = 25.0
PREEMPTIVE_RECONNECT_SEC = 58 * 60
BACKOFF_SECS = [0.5, 1.0, 2.0, 5.0, 10.0]


@dataclass
class SseEvent:
    type: Literal["snapshot", "trade", "market_status", "ping"]
    seq: int
    data: dict


class SseClient:
    def __init__(
        self,
        *,
        base_url: str,
        token_manager: TokenManager,
        raw_client: httpx.AsyncClient | None = None,
    ):
        self._base_url = base_url
        self._tm = token_manager
        self._client = raw_client or httpx.AsyncClient(base_url=base_url, timeout=None)

    async def subscribe(self, market_id: int) -> AsyncIterator[SseEvent]:
        backoff_idx = 0
        last_seq = -1
        while True:
            try:
                token = await self._tm.get_valid_access()
                headers = {"Authorization": f"Bearer {token}",
                           "Accept": "text/event-stream"}
                connect_at = time.monotonic()
                last_event_at = time.monotonic()
                async with self._client.stream(
                    "GET", f"/api/v1/stream/market/{market_id}",
                    headers=headers,
                ) as resp:
                    resp.raise_for_status()
                    backoff_idx = 0  # 连上就重置退避
                    async for event in _parse_sse_stream(resp):
                        # gap detection（非 ping）
                        if event.type != "ping":
                            if event.type == "snapshot":
                                last_seq = event.seq
                                yield event
                            elif event.seq == last_seq + 1:
                                last_seq = event.seq
                                yield event
                            else:
                                _log.warning("sse_gap_reconnect",
                                             market_id=market_id,
                                             expected=last_seq + 1,
                                             got=event.seq)
                                last_seq = -1
                                break  # 关连接重连
                        last_event_at = time.monotonic()

                        # 58 min 主动重连
                        if time.monotonic() - connect_at > PREEMPTIVE_RECONNECT_SEC:
                            _log.info("sse_preemptive_reconnect",
                                      market_id=market_id)
                            break
            except httpx.TimeoutException:
                _log.info("sse_timeout_reconnect", market_id=market_id)
            except (httpx.HTTPError, httpx.HTTPStatusError) as e:
                _log.error("sse_http_error", market_id=market_id, error=str(e))
                await asyncio.sleep(BACKOFF_SECS[min(backoff_idx, len(BACKOFF_SECS) - 1)])
                backoff_idx += 1
                continue
            except Exception as e:
                _log.exception("sse_unknown_error", market_id=market_id, error=str(e))
                await asyncio.sleep(BACKOFF_SECS[min(backoff_idx, len(BACKOFF_SECS) - 1)])
                backoff_idx += 1
                continue

            # 正常断开（gap / preemptive / 服务端 EOF）→ 立刻重连
            # 重连后第一帧若是 snapshot 且 last_seq == -1 表示 gap recover
            if last_seq == -1:
                # 标记给下一次 snapshot 加 gap_recover
                self._next_snapshot_gap_recover = True
            else:
                self._next_snapshot_gap_recover = False

    # 注意：把 gap_recover 标记放在 SseClient 实例上有竞态，简化做法是
    # 让 _parse_sse_stream 不知道这个事，由 SseClient 在 yield 前 patch。
    # 上面循环里改造 yield 即可（在 break 后下一轮 snapshot 时 patch data）。


async def _parse_sse_stream(resp) -> AsyncIterator[SseEvent]:
    """SSE wire format 子集解析。

    后端只用 `event:` 和 `data:` 两行 + 空行分隔块。
    25s 无任何行视为 zombie，抛 TimeoutException 让上层重连。
    """
    cur_event_type = None
    cur_data = None
    last_line_at = time.monotonic()

    try:
        async for line in _aiter_lines_with_timeout(resp, ZOMBIE_TIMEOUT_SEC):
            line = line.rstrip("\r")
            if not line:
                # 块结束，emit 一个 event
                if cur_event_type and cur_data is not None:
                    try:
                        payload = json.loads(cur_data)
                    except json.JSONDecodeError:
                        _log.warning("sse_parse_failed", line=cur_data[:100])
                        cur_event_type = cur_data = None
                        continue
                    yield SseEvent(
                        type=payload.get("type", cur_event_type),
                        seq=int(payload.get("seq", 0)),
                        data=payload.get("data", {}),
                    )
                cur_event_type = cur_data = None
                continue
            if line.startswith(":"):
                continue  # comment
            if line.startswith("event:"):
                cur_event_type = line[6:].strip()
            elif line.startswith("data:"):
                cur_data = line[5:].strip()
    except asyncio.TimeoutError:
        raise httpx.TimeoutException("sse zombie timeout")


async def _aiter_lines_with_timeout(resp, timeout: float):
    """asyncio.wait_for 包 aiter_lines；timeout 内无新行抛 TimeoutError。"""
    it = resp.aiter_lines().__aiter__()
    while True:
        line = await asyncio.wait_for(it.__anext__(), timeout=timeout)
        yield line
```

**注意**：上述 SseClient 里 `gap_recover` 的传递有简化写法的复杂度。
更干净的实现：把 last_seq 状态保存为局部变量，重连后 yield snapshot 时
直接 patch event.data 加 `gap_recover=True`。改写 subscribe 的循环：

```python
    async def subscribe(self, market_id: int) -> AsyncIterator[SseEvent]:
        backoff_idx = 0
        last_seq = -1
        next_is_gap_recover = False
        while True:
            try:
                token = await self._tm.get_valid_access()
                headers = {"Authorization": f"Bearer {token}",
                           "Accept": "text/event-stream"}
                connect_at = time.monotonic()
                async with self._client.stream(
                    "GET", f"/api/v1/stream/market/{market_id}",
                    headers=headers,
                ) as resp:
                    resp.raise_for_status()
                    backoff_idx = 0
                    async for event in _parse_sse_stream(resp):
                        if event.type == "snapshot":
                            if next_is_gap_recover:
                                event.data["gap_recover"] = True
                                next_is_gap_recover = False
                            last_seq = event.seq
                            yield event
                        elif event.type == "ping":
                            pass  # 不 yield，仅重置 zombie timer（在 parse 里）
                        else:
                            if event.seq == last_seq + 1:
                                last_seq = event.seq
                                yield event
                            else:
                                _log.warning("sse_gap_reconnect",
                                             market_id=market_id,
                                             expected=last_seq + 1,
                                             got=event.seq)
                                next_is_gap_recover = True
                                last_seq = -1
                                break
                        if time.monotonic() - connect_at > PREEMPTIVE_RECONNECT_SEC:
                            _log.info("sse_preemptive_reconnect", market_id=market_id)
                            break
            except httpx.TimeoutException:
                _log.info("sse_timeout_reconnect", market_id=market_id)
                next_is_gap_recover = True
                last_seq = -1
            except httpx.HTTPError as e:
                _log.error("sse_http_error", market_id=market_id, error=str(e))
                await asyncio.sleep(BACKOFF_SECS[min(backoff_idx, len(BACKOFF_SECS) - 1)])
                backoff_idx += 1
                next_is_gap_recover = True
                last_seq = -1
                continue
            except Exception as e:
                _log.exception("sse_unknown_error", market_id=market_id, error=str(e))
                await asyncio.sleep(BACKOFF_SECS[min(backoff_idx, len(BACKOFF_SECS) - 1)])
                backoff_idx += 1
                next_is_gap_recover = True
                last_seq = -1
                continue
```

**只采用第二版**（删掉第一版的 subscribe 实现，保留 `_parse_sse_stream` 和 `_aiter_lines_with_timeout`）。

- [ ] **Step 4: 跑测试 + 全套**

```bash
pytest tests/test_sse_client.py -v
pytest 2>&1 | tail -3
```
Expected: 2 sse client tests passed；全套 50 + 2 = 52 passed

- [ ] **Step 5: Commit**

```bash
cd /data/sunyunbo/www/TouhouCCB
git add quant/thccb_quant/client/sse.py quant/tests/test_sse_client.py
git commit -m "feat(quant): SseClient 真实现（wire 解析+gap 重连+主动重连）"
```

---

## Task 4: SseSubscriber + trader.py 集成

**Files:**
- Create: `quant/thccb_quant/client/sse_subscriber.py`
- Modify: `quant/thccb_quant/trader.py`
- Create: `quant/tests/test_sse_subscriber.py`

- [ ] **Step 1: 写测试 `quant/tests/test_sse_subscriber.py`**

```python
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

from thccb_quant.client.sse import SseEvent
from thccb_quant.client.sse_subscriber import SseSubscriber
from thccb_quant.state.store import Store


@pytest.fixture
async def store(tmp_path: Path):
    s = await Store.open(tmp_path / "t.db")
    yield s
    await s.close()


def _trade_event(seq, market_id, trade_id, outcome_id=1):
    return SseEvent(
        type="trade", seq=seq,
        data={"trade": {
            "id": trade_id, "type": "BUY", "outcome_id": outcome_id,
            "username": "u", "shares": 1.0, "price": 0.5,
            "gross": 0.5, "fee": 0.0, "post_market_price": 0.5,
            "market_prices_post": [0.5, 0.5],
            "timestamp": "2026-05-17T07:00:00Z",
        }},
    )


def _mk_strategy(name: str, market_id: int):
    s = MagicMock()
    s.name = name
    s.market_id = market_id
    s.on_sse_event = AsyncMock()
    return s


async def _make_sub(store, sse_events_per_market, strategies, market_ids):
    """sse_events_per_market: dict[market_id, list[SseEvent | Exception]]
    SseClient.subscribe 模拟成"按顺序 yield 这些 event 然后结束"。
    """
    sse = MagicMock()
    async def subscribe(market_id):
        for ev in sse_events_per_market.get(market_id, []):
            if isinstance(ev, Exception):
                raise ev
            yield ev
    sse.subscribe = subscribe
    rest = MagicMock()
    rest.get_recent_trades = AsyncMock(return_value=[])
    return SseSubscriber(
        rest=rest, store=store, sse_client=sse,
        strategies=strategies, market_ids=market_ids,
        logger=structlog.get_logger("test"),
    )


async def test_trade_event_writes_table_and_dispatches(store):
    strat = _mk_strategy("s1", market_id=1)
    sub = await _make_sub(
        store,
        sse_events_per_market={1: [_trade_event(1, 1, trade_id=10)]},
        strategies=[strat],
        market_ids={1},
    )
    await asyncio.wait_for(sub.run(), timeout=2.0)
    rows = await store.recent_trades_observed(market_id=1, limit=10)
    assert len(rows) == 1
    assert rows[0]["trade_id"] == 10
    strat.on_sse_event.assert_called_once()


async def test_dispatch_routes_by_market_id(store):
    s1 = _mk_strategy("s1", market_id=1)
    s2 = _mk_strategy("s2", market_id=2)
    sub = await _make_sub(
        store,
        sse_events_per_market={1: [_trade_event(1, 1, trade_id=10)]},
        strategies=[s1, s2],
        market_ids={1},
    )
    await asyncio.wait_for(sub.run(), timeout=2.0)
    s1.on_sse_event.assert_called_once()
    s2.on_sse_event.assert_not_called()


async def test_strategy_exception_isolated(store):
    s1 = _mk_strategy("s1", market_id=1)
    s1.on_sse_event = AsyncMock(side_effect=RuntimeError("boom"))
    s2 = _mk_strategy("s2", market_id=1)
    sub = await _make_sub(
        store,
        sse_events_per_market={1: [_trade_event(1, 1, trade_id=10)]},
        strategies=[s1, s2],
        market_ids={1},
    )
    # 不应抛
    await asyncio.wait_for(sub.run(), timeout=2.0)
    s1.on_sse_event.assert_called_once()
    s2.on_sse_event.assert_called_once()


async def test_preload_partial_trades(store):
    sse = MagicMock()
    async def subscribe(market_id):
        return
        yield
    sse.subscribe = subscribe
    rest = MagicMock()
    rest.get_recent_trades = AsyncMock(return_value=[
        MagicMock(model_dump=lambda: {
            "id": 100, "outcome_id": 1, "type": "BUY", "shares": "1.0",
            "price": "0.5", "username": "u1",
            "timestamp": "2026-05-17T07:00:00Z",
            "market_id": 1, "market_title": "M", "outcome_label": "yes",
        }),
    ])
    sub = SseSubscriber(
        rest=rest, store=store, sse_client=sse,
        strategies=[], market_ids={1},
        logger=structlog.get_logger("test"),
    )
    await asyncio.wait_for(sub.run(), timeout=2.0)
    cur = await store._conn.execute("SELECT count(*) FROM partial_trades")
    n = (await cur.fetchone())[0]
    assert n == 1
```

- [ ] **Step 2: 跑测试确认 FAIL**

```bash
pytest tests/test_sse_subscriber.py -v
```
Expected: ModuleNotFoundError sse_subscriber

- [ ] **Step 3: 写 `quant/thccb_quant/client/sse_subscriber.py`**

```python
"""SseSubscriber: 协调多 market SSE 订阅 + 写 trades 表 + dispatch
on_sse_event。spec §6。
"""
import asyncio
from typing import Iterable

import structlog

from thccb_quant.client.rest import RestClient
from thccb_quant.client.sse import SseClient, SseEvent
from thccb_quant.state.store import Store
from thccb_quant.strategy.base import Strategy


class SseSubscriber:
    def __init__(
        self,
        *,
        rest: RestClient,
        store: Store,
        sse_client: SseClient,
        strategies: list[Strategy],
        market_ids: Iterable[int],
        logger: structlog.BoundLogger,
    ):
        self._rest = rest
        self._store = store
        self._sse = sse_client
        self._strategies = list(strategies)
        self._market_ids = set(market_ids)
        self._log = logger

    async def run(self) -> None:
        await self._preload_partial_trades()
        if not self._market_ids:
            self._log.info("sse_no_markets_to_subscribe")
            return
        tasks = [
            asyncio.create_task(self._watch_market(mid))
            for mid in self._market_ids
        ]
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _preload_partial_trades(self) -> None:
        try:
            trades = await self._rest.get_recent_trades(limit=100)
            items = [t.model_dump() for t in trades]
            inserted = await self._store.bulk_insert_partial_trades(items)
            self._log.info("sse_partial_trades_preloaded",
                           returned=len(items), inserted=inserted)
        except Exception:
            self._log.exception("sse_partial_trades_preload_failed_continuing")

    async def _watch_market(self, market_id: int) -> None:
        async for event in self._sse.subscribe(market_id):
            try:
                await self._handle_event(market_id, event)
            except Exception:
                self._log.exception("sse_handle_event_failed",
                                    market_id=market_id, seq=event.seq)

    async def _handle_event(self, market_id: int, event: SseEvent) -> None:
        if event.type == "trade":
            await self._store.log_trade(market_id=market_id, payload=event.data)
            await self._dispatch(market_id, event)
        elif event.type == "market_status":
            self._log.info("sse_market_status",
                           market_id=market_id,
                           status=event.data.get("status"))
            await self._dispatch(market_id, event)
        elif event.type == "snapshot":
            self._log.info("sse_snapshot_bootstrapped",
                           market_id=market_id, seq=event.seq,
                           gap_recover=event.data.get("gap_recover", False))

    async def _dispatch(self, market_id: int, event: SseEvent) -> None:
        for s in self._strategies:
            if getattr(s, "market_id", None) != market_id:
                continue
            try:
                await s.on_sse_event(event)
            except Exception:
                self._log.exception("sse_on_sse_event_failed",
                                    strategy=s.name, market_id=market_id)
```

- [ ] **Step 4: 改 `quant/thccb_quant/trader.py` 集成 SseSubscriber**

在 imports 区追加：

```python
import httpx as _httpx_for_sse  # 已经 import httpx，复用即可，这行只是表意
from thccb_quant.client.sse import SseClient
from thccb_quant.client.sse_subscriber import SseSubscriber
```
（实际上只加 `from thccb_quant.client.sse import SseClient` 和 `from thccb_quant.client.sse_subscriber import SseSubscriber` 两行；不需要重复 httpx import。）

把 `_run_strategy` 函数拆成两个：

```python
async def _setup_strategy(strategy, ctx: StrategyContext, logger):
    try:
        await strategy.setup(ctx)
    except Exception:
        logger.exception("strategy_setup_failed", strategy=strategy.name)
        raise


async def _run_strategy_loop(strategy, ctx: StrategyContext, logger):
    """已经 setup 过，仅跑 tick 循环 + teardown。"""
    try:
        while not _stop_event.is_set():
            try:
                await strategy.tick()
            except FatalAuthError:
                logger.error("strategy_hit_fatal_auth_error_stopping_trader")
                _stop_event.set()
                raise
            except Exception as e:
                logger.exception("strategy_tick_failed", error=str(e))
            try:
                await asyncio.wait_for(
                    _stop_event.wait(), timeout=strategy.tick_interval_sec
                )
            except asyncio.TimeoutError:
                pass
    finally:
        try:
            await strategy.teardown()
        except Exception:
            logger.exception("strategy_teardown_failed")
```

删掉旧的 `_run_strategy` 函数（替换为上面两个）。

在 `main_async` 里：找到 `_install_signal_handlers()` 调用之前的策略实例化区域，替换为：

```python
    _install_signal_handlers()

    tasks = [
        asyncio.create_task(_kill_switch_watcher(logger)),
        asyncio.create_task(_refresh_token_warner(token_mgr, logger)),
    ]

    # 实例化所有 enabled 策略
    enabled_pairs = []  # [(strategy, ctx, strategy_logger), ...]
    for s_cfg in config["strategies"]:
        if not s_cfg.get("enabled", False):
            continue
        cls = get_strategy_class(s_cfg["type"])
        strat = cls(name=s_cfg["name"], config=s_cfg)
        s_logger = get_logger(s_cfg["name"], strategy=s_cfg["name"])
        ctx = StrategyContext(
            rest=rest, broker=broker, store=store, logger=s_logger,
            config={**config["risk"], **s_cfg},
        )
        enabled_pairs.append((strat, ctx, s_logger))

    # 先 setup 所有策略（让 market_id 落定）
    for strat, ctx, s_logger in enabled_pairs:
        await _setup_strategy(strat, ctx, s_logger)

    # 计算 SSE 需要订阅的 market_ids
    market_ids = {s.market_id for s, _, _ in enabled_pairs if s.market_id is not None}
    if market_ids:
        sse_raw = httpx.AsyncClient(base_url=base_url, timeout=None)
        sse_client = SseClient(
            base_url=base_url, token_manager=token_mgr, raw_client=sse_raw,
        )
        subscriber = SseSubscriber(
            rest=rest, store=store, sse_client=sse_client,
            strategies=[s for s, _, _ in enabled_pairs],
            market_ids=market_ids,
            logger=get_logger("sse"),
        )
        tasks.append(asyncio.create_task(subscriber.run()))
    else:
        sse_raw = None

    # 起策略 tick 循环
    for strat, ctx, s_logger in enabled_pairs:
        tasks.append(asyncio.create_task(_run_strategy_loop(strat, ctx, s_logger)))

    await _stop_event.wait()
    logger.info("shutting_down")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    await store.close()
    await api_client.aclose()
    await raw_client.aclose()
    if sse_raw is not None:
        await sse_raw.aclose()
    logger.info("stopped_clean")
    return 0
```

（注意：删掉原 main_async 里旧的"实例化策略并起 task"那一段。）

- [ ] **Step 5: 跑测试 + 全套 + smoke import**

```bash
cd /data/sunyunbo/www/TouhouCCB/quant && source .venv/bin/activate
pytest tests/test_sse_subscriber.py -v
python -c "from thccb_quant.trader import main_async; print('import ok')"
pytest 2>&1 | tail -3
```
Expected: 4 sse subscriber tests passed；import ok；全套 52 + 4 = 56 passed

- [ ] **Step 6: Commit**

```bash
cd /data/sunyunbo/www/TouhouCCB
git add quant/thccb_quant/client/sse_subscriber.py quant/thccb_quant/trader.py \
        quant/tests/test_sse_subscriber.py
git commit -m "feat(quant): SseSubscriber + trader 集成（启动顺序拆 setup/tick）"
```

---

## Spec Coverage Check

| Spec 章节 | 实现任务 |
|---|---|
| §2 调研约束（无 Last-Event-ID/seq gap/字段不齐） | 设计已遵循 |
| §3 整体架构（trader spawn SseSubscriber） | Task 4 |
| §4 模块清单 | Task 1-4 |
| §5 SseClient | Task 3 |
| §6 SseSubscriber | Task 4 |
| §7 trades + partial_trades schema | Task 1 |
| §8 Store 新方法 | Task 1 |
| §9 RestClient.get_recent_trades | Task 1 |
| §10 trader.py 集成 + 启动顺序 | Task 4 |
| §11 错误处理 | Task 3 (SseClient 重试 + log) + Task 4 (preload fail / on_sse_event isolation) |
| §12 测试 | Task 1/3/4 含测试文件 |
| §13 范围外 | 未实现（YAGNI） |
