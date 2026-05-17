# SSE 数据管道 Phase 1 设计

**日期**: 2026-05-17
**作者**: renko6626
**状态**: 设计稿（待实现）
**前置**: `docs/superpowers/specs/2026-05-17-quant-trader-design.md`（量化脚本主 spec）
**取代**: 主 spec §4.3「SseClient skeleton（不启用）」整节

## 1. 背景与目标

主 spec 把 SSE 当 skeleton 推到 Phase 2 再做；现在 Phase 1 提前实施，原因：

- thccb 市场波动剧烈（实测/用户反馈：单笔成交可让价格 ±10%）
  详见 memory [project-market-volatility]
- 当前策略 60s 一次 REST polling 在该波动量级下信息丢失严重
- 想用真实成交流（含散户名/时间分布/价格冲击）做短线策略研究，
  即 Phase 2 的"收割散户"目标需要 Phase 1 的真实数据
- 后端 SSE 端点 `/api/v1/stream/market/{id}` 已经存在且推送完整 trade
  事件（含 `trade.id`、`username`、`market_prices_post`），不实现等于
  浪费现成基础设施

**Phase 1 目标**：

1. 实现 SseClient（替换主 spec §4.3 skeleton）— 单 market 长连接、
   自动重连、gap detection
2. 实现 SseSubscriber — 多 market 协调、写 `trades` 表持久化、
   dispatch 给 `Strategy.on_sse_event`
3. 启动时通过 `recent-trades` 端点预加载 100 条历史到 `partial_trades`
   表（字段稀疏，热启动看市场近况用）
4. 全部 SSE 错误"默默重试 + log"，永不影响策略 polling 主路径

**Phase 1 非目标**：

- 写具体的 SSE-driven 短线策略（留 Phase 2）
- 后端 SSE 协议任何改动
- 跨 trader 进程的事件转发
- 业务级的 Prometheus 指标 / Grafana 看板
- 从超过 100 条之外的更深历史回填（后端 `/recent-trades` 无 since 参数）

## 2. 调研结论（决定设计的硬约束）

来自 Explore subagent 对 `backend/app/api/v1/stream.py`、`backend/app/services/realtime.py`
的代码级调研：

| 约束 | 影响 |
|---|---|
| **无 `Last-Event-ID` header 支持** | 重连后断点之间事件**无法补发**，必须重新 bootstrap snapshot |
| **`seq` 字段 per-market 单调递增** | 客户端可检测 gap（`event.seq != lastSeq+1`），但只能用"重连重 bootstrap"应对 |
| **`MessageBroker` 是纯内存队列**（`realtime.py:36-39`） | 后端重启 = 所有客户端 seq 重置；客户端需容忍 |
| **心跳 25s**（`stream.py:124`） | 客户端 60s 读超时兜底比心跳宽，避免误判 |
| **强断 1h**（`stream.py:20` `MAX_SSE_DURATION=3600`） | 客户端在 58 min 时主动重连，避免硬断瞬间丢事件 |
| **同用户 SSE 连接无限制** | 多 market 多连接安全 |
| **`recent-trades` 字段不齐**（缺 `market_prices_post`/`gross`/`fee`/`post_market_price`） | 不能直接 INSERT 到 `trades` 表，需独立 `partial_trades` 表 |
| **`recent-trades` 无 `since_ts` 参数** | 仅启动时一次性 limit=100 预加载，不做后续增量补 |
| **SSE 端点未要求认证**（`stream.py:74` 无 `Depends(user)`） | 客户端仍带 `Authorization: Bearer`，后端日后加鉴权零迁移成本 |

## 3. 整体架构

```
┌─ trader.py ─────────────────────────────────────────────┐
│                                                          │
│  asyncio.gather(                                         │
│    _kill_switch_watcher,                                 │
│    _refresh_token_warner,                                │
│    *strategy_tasks,                                      │
│    SseSubscriber.run(),  ← 新增 task                     │
│  )                                                        │
└──────────────────────────┬──────────────────────────────┘
                           │
                  ┌────────▼────────┐
                  │ SseSubscriber    │
                  │  (协调 N market) │
                  └────────┬────────┘
                           │  for each market_id:
                  ┌────────▼─────────┐
                  │ SseClient         │
                  │  .subscribe(mid)  │
                  │  → AsyncIterator  │
                  └────────┬─────────┘
                           │ event
        ┌──────────────────┼────────────────────┐
        ▼                  ▼                    ▼
  store.log_trade()  dispatch on_sse_event   log to system.jsonl
  (trades 表)        (关心该 market 的策略)   (snapshot/status)
```

## 4. 模块清单

| 文件 | 操作 | 内容 |
|---|---|---|
| `quant/thccb_quant/client/sse.py` | **重写**（替换 skeleton） | SseClient + SseEvent dataclass |
| `quant/thccb_quant/client/sse_subscriber.py` | **新建** | SseSubscriber + preload 逻辑 |
| `quant/thccb_quant/client/rest.py` | **修改** | 加 `get_recent_trades` + `PartialTrade` 模型 |
| `quant/thccb_quant/state/schema.sql` | **修改** | 加 `trades` / `partial_trades` 表 |
| `quant/thccb_quant/state/store.py` | **修改** | 加 `log_trade` / `bulk_insert_partial_trades` / `recent_trades_observed` |
| `quant/thccb_quant/strategy/base.py` | **修改** | Strategy ABC 加 `market_id: int \| None` 属性（默认 None；策略 setup 内必须设置） |
| `quant/thccb_quant/strategy/dca.py` | **修改** | setup 内 `self.market_id = int(self._config["market_id"])`（config 新加必填字段） |
| `quant/config.example.yaml` | **修改** | DCA 策略示例补 `market_id: 1` 必填字段 |
| `quant/thccb_quant/strategy/grid.py` | **修改** | setup 里把已有的 `self._market_id` 同步到 `self.market_id` |
| `quant/thccb_quant/trader.py` | **修改** | 调整启动顺序：strategies setup 先于 SSE 启动；启动 SseSubscriber task；shutdown cancel |
| `quant/tests/test_sse_client.py` | **新建** | wire format 解析、gap detection、重连 |
| `quant/tests/test_sse_subscriber.py` | **新建** | dispatch 路由、preload、错误隔离 |
| `quant/tests/test_store_trades.py` | **新建** | log_trade / partial_trades 表读写 |

## 5. SseClient

```python
# quant/thccb_quant/client/sse.py

from dataclasses import dataclass
from typing import AsyncIterator, Literal


@dataclass
class SseEvent:
    type: Literal["snapshot", "trade", "market_status", "ping"]
    seq: int
    data: dict


class SseClient:
    """长连接 SSE 客户端，单 market 一个实例。永不退出（除非外部 cancel）。

    自动处理：
    - SSE wire format 解析（`event: X\\ndata: Y\\n\\n`）
    - 25 s 无任何事件（含 ping）→ 视为僵尸，重连
    - 58 min 主动重连（避免后端 1 h 强断瞬间丢事件）
    - seq gap detection（`event.seq != lastSeq + 1`）→ 重连重 bootstrap
    - 网络错误指数退避：0.5 / 1 / 2 / 5 / 10 s 封顶
    - 永远带 `Authorization: Bearer`（后端目前不强制但保险）
    """

    def __init__(
        self,
        *,
        base_url: str,
        token_manager: TokenManager,
        raw_client: httpx.AsyncClient | None = None,
    ):
        # raw_client 缺省时内部建一个 httpx.AsyncClient（独立于 RestClient
        # 的客户端，避免长连接占用 RestClient 的连接池）
        ...

    async def subscribe(self, market_id: int) -> AsyncIterator[SseEvent]:
        """订阅一个 market，无限循环 yield SseEvent。

        gap detection 触发重连时，客户端会先 yield 一个内部的 snapshot
        event（标 `gap_recover=True` 在 data 里），让上层知道刚发生了
        重 bootstrap、之前缓存的派生状态需要清除。
        """
```

### 5.1 SSE wire format 解析

最小子集（thccb 后端只用 `event:` 和 `data:` 两行 + 空行分隔）：

```
event: trade
data: {"type":"trade","seq":42,"data":{"trade":{...}}}

event: ping
data: {...}
```

解析器按 `\n\n` 切块；每块按 `\n` 切行；`event:` 给类型，`data:` 给
JSON。忽略 comment 行（`:` 开头）。

### 5.2 重连状态机

```
START
  ↓
CONNECT  ──network err──→  BACKOFF (exp 0.5/1/2/5/10s)  ──→  CONNECT
  ↓ 200 OK
RECV snapshot
  ↓ 记 lastSeq = snapshot.seq
RECV event
  ├─ seq == lastSeq+1  → yield event; lastSeq++
  ├─ seq != lastSeq+1  → DISCONNECT; CONNECT（gap recover）
  ├─ 25s timeout       → DISCONNECT; CONNECT（zombie）
  └─ 58min 已过         → DISCONNECT; CONNECT（preemptive）
```

ping 事件 `seq=0`（`realtime.py:81-82`），不参与 lastSeq 累加，仅重置
zombie timeout。

## 6. SseSubscriber

```python
# quant/thccb_quant/client/sse_subscriber.py

class SseSubscriber:
    """协调多 market SSE 订阅 + 持久化 + dispatch on_sse_event。

    生命周期由 trader.py 拥有；trader 主循环 `asyncio.gather` 一个
    `subscriber.run()` task；shutdown 时 cancel。
    """

    def __init__(
        self,
        *,
        rest: RestClient,
        store: Store,
        sse_client: SseClient,
        strategies: list[Strategy],
        market_ids: set[int],
        logger: structlog.BoundLogger,
    ):
        # market_ids 由 trader.py 计算：所有 enabled 策略引用的
        # market_id 并集（grid 用 config.market_id；dca 通过
        # outcome_id 反查 market_id —— 启动时 rest.get_market 一次）
        ...

    async def run(self) -> None:
        # 1. preload partial_trades（best-effort，失败 log + 继续）
        # 2. 对每个 market_id 起一个 _watch_market task，asyncio.gather
        # 3. 任何 _watch_market task 抛异常 → log + 不影响其他 + 不传播

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
            for s in self._strategies_for(market_id):
                try:
                    await s.on_sse_event(event)
                except Exception:
                    self._log.exception("on_sse_event_failed",
                                        strategy=s.name, market_id=market_id)
        elif event.type == "market_status":
            self._log.info("sse_market_status",
                           market_id=market_id, status=event.data.get("status"))
            for s in self._strategies_for(market_id):
                try:
                    await s.on_sse_event(event)
                except Exception:
                    self._log.exception("on_sse_event_failed", ...)
        elif event.type == "snapshot":
            self._log.info("sse_snapshot_bootstrapped",
                           market_id=market_id, seq=event.seq,
                           gap_recover=event.data.get("gap_recover", False))
        # ping 不落盘也不 dispatch

    def _strategies_for(self, market_id: int) -> list[Strategy]:
        # 简单：遍历 self._strategies，留 s.market_id == market_id 的
        # 前置条件：所有策略已 setup（market_id 已被设置）— trader.py
        # 负责保证启动顺序
        return [s for s in self._strategies if getattr(s, "market_id", None) == market_id]

    async def _preload_partial_trades(self) -> None:
        try:
            trades = await self._rest.get_recent_trades(limit=100)
            await self._store.bulk_insert_partial_trades(trades)
            self._log.info("partial_trades_preloaded", count=len(trades))
        except Exception:
            self._log.exception("partial_trades_preload_failed_continuing")
```

**注意**：当前 `RestClient` 没有 `get_recent_trades` 方法（主 spec
§4.2 表里只列了 `recent-trades` 但 Task 7 未实现）。SSE Phase 1 需要
**给 RestClient 增加** `async def get_recent_trades(limit: int) -> list[PartialTrade]`，
对应后端 `GET /api/v1/market/recent-trades?limit=N`。

## 7. Schema 增量

```sql
-- 追加到 quant/thccb_quant/state/schema.sql

CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trade_id INTEGER NOT NULL UNIQUE,
  ts TEXT NOT NULL,                       -- server timestamp (ISO8601)
  ingest_ts TEXT NOT NULL,                -- 本地 ingest 时间，用于诊断延迟
  market_id INTEGER NOT NULL,
  outcome_id INTEGER NOT NULL,
  side TEXT NOT NULL,                     -- 'BUY' | 'SELL'（后端原样大写）
  shares TEXT NOT NULL,                   -- Decimal 6 位字符串
  price TEXT NOT NULL,                    -- Decimal 8 位字符串
  gross TEXT NOT NULL,                    -- 手续费前
  fee TEXT NOT NULL,                      -- 后端手续费
  username TEXT,                          -- 散户用户名（可能空）
  post_market_price TEXT NOT NULL,        -- 目标 outcome 成交后价
  market_prices_post_json TEXT NOT NULL   -- 全市场 prices_post JSON array
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

**`trade_id` UNIQUE 两表独立**：同一 trade_id 启动时进 partial_trades，
SSE 推送的同一笔进 trades，两表互不冲突。复盘查询用：

```sql
SELECT * FROM trades WHERE market_id=1
UNION ALL
SELECT trade_id, ts, market_id, outcome_id, side, shares, price, username, NULL, NULL, NULL, NULL, NULL
  FROM partial_trades WHERE market_id=1 AND trade_id NOT IN (SELECT trade_id FROM trades)
ORDER BY ts;
```

INSERT 用 `INSERT OR IGNORE` 防重连后重收同一 trade_id 重复。

## 8. Store 新方法

```python
# 追加到 quant/thccb_quant/state/store.py

async def log_trade(self, *, market_id: int, payload: dict) -> None:
    """从 SSE trade event 的 data 字段写一行 trades 表。
    
    payload 形如 {"trade": {...}}; 内部解 trade 字段。
    INSERT OR IGNORE 防重连后重复。
    """

async def bulk_insert_partial_trades(self, trades: list[PartialTrade]) -> int:
    """批量 INSERT OR IGNORE，返回实际插入条数。"""

async def recent_trades_observed(
    self, *, market_id: int, limit: int = 50
) -> list[dict]:
    """读 trades 表最近 N 条，DESC by ts。复盘 / 调试用。"""
```

## 9. RestClient 新方法

```python
# 追加到 quant/thccb_quant/client/rest.py

class PartialTrade(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int                     # trade_id
    outcome_id: int
    type: str                   # 'BUY' | 'SELL'
    shares: Decimal
    price: Decimal
    username: str | None = None
    timestamp: str              # ISO8601
    market_id: int
    market_title: str | None = None
    outcome_label: str | None = None


async def get_recent_trades(self, *, limit: int = 100) -> list[PartialTrade]:
    r = await self._request(
        "GET", "/api/v1/market/recent-trades",
        params={"limit": limit},
    )
    return [PartialTrade.model_validate(x) for x in r.json()]
```

## 10. trader.py 集成点

启动顺序必须调整为：**先 setup 所有 strategy，再起 SSE subscriber**。
原因：SseSubscriber 用 `strategy.market_id` 做事件路由，而 DCA 的
`market_id` 是在 `setup()` 里通过 `rest.get_market` 反查得到的，setup
没跑过它就是 None。

```python
# main_async 里改造：

# 1. 实例化所有 enabled 策略（构造函数已经能读 config，但 market_id 还没定）
enabled_strategies = []
for s_cfg in config["strategies"]:
    if not s_cfg.get("enabled", False):
        continue
    cls = get_strategy_class(s_cfg["type"])
    enabled_strategies.append(cls(name=s_cfg["name"], config=s_cfg))

# 2. 给每个策略建 context（同前）

# 3. 先 setup 所有策略（注意：不是 _run_strategy 里 setup → tick → 循环，
#    而是显式先 setup 全部，setup 完才进 tick 循环）
for strat, ctx in zip(enabled_strategies, contexts):
    await strat.setup(ctx)

# 4. 这时所有策略的 .market_id 都已设置
market_ids = {s.market_id for s in enabled_strategies if s.market_id is not None}

# 5. 如果至少有一个 market 要订阅，起 SseSubscriber
if market_ids:
    sse_raw_client = httpx.AsyncClient(base_url=base_url, timeout=None)
    sse_client = SseClient(
        base_url=base_url, token_manager=token_mgr,
        raw_client=sse_raw_client,
    )
    subscriber = SseSubscriber(
        rest=rest, store=store, sse_client=sse_client,
        strategies=enabled_strategies,
        market_ids=market_ids,
        logger=get_logger("sse"),
    )
    tasks.append(asyncio.create_task(subscriber.run()))

# 6. 启动 strategy tick loops（注意：现在 _run_strategy 跳过 setup，已经做过了）
for strat, ctx in zip(enabled_strategies, contexts):
    tasks.append(asyncio.create_task(_run_strategy_loop_only(strat, ctx, logger)))
```

**对 `_run_strategy` 的修改**：拆成两个函数：

- `_setup_strategy(strat, ctx)` — 仅调 `await strat.setup(ctx)`（错误传播给 trader）
- `_run_strategy_loop_only(strat, ctx, logger)` — 删掉 setup 调用，仅跑 tick 循环 + teardown

**`Strategy.market_id` 约定**：

- 在 `strategy/base.py` 加 `market_id: int | None = None` 类属性
- `GridStrategy.setup`：第一行写 `self.market_id = self._market_id`（已经在 init 里读了 config["market_id"]）
- `DcaStrategy.setup`：在已有 `_spent` 重放代码后加：
  ```python
  market = await ctx.rest.get_market_for_outcome(self._outcome_id)
  # rest 加一个辅助方法 get_market_for_outcome：list_markets 遍历找
  self.market_id = market.id
  ```
  或更简单：DCA config 直接加可选 `market_id` 字段，setup 优先读 config，否则 fallback 反查。**推荐方案**：DCA config 加 `market_id` 字段（README 示例同步更新），setup 读它；这样不需要反查、不需要 RestClient 加新辅助方法，零运行时网络成本。

**最终决定**：DCA config schema 加 `market_id`（必填）。`config.example.yaml`
里 dca 示例的 `outcome_id: 1` 旁边补一行 `market_id: 1`。Strategy
ABC 加 `market_id: int | None = None`；DcaStrategy.setup 设
`self.market_id = int(self._config["market_id"])`；GridStrategy.setup
同步 `self.market_id = self._market_id`。零反查、零新 API。

## 11. 错误处理总表

| 场景 | 处理 |
|---|---|
| SSE 初连失败（网络/DNS） | log ERROR；指数退避重试，永不退出 |
| SSE 鉴权失败（401/403） | log ERROR；不重试 5 次；继续重试但每 10 次 ERROR 提醒 |
| seq gap detected | log INFO + 重连重 bootstrap；策略可能接到一个 `gap_recover=True` 的 snapshot 事件 |
| 25s 心跳超时 | log INFO "zombie_reconnect"；重连 |
| 58 min 主动重连 | log INFO "preemptive_reconnect"；重连 |
| recent-trades preload 失败 | log WARN "partial_trades_preload_failed_continuing"；跳过预加载继续启动 |
| `log_trade` SQL 异常（如 trade_id 已存 — INSERT OR IGNORE 应避免） | log ERROR；不影响 dispatch on_sse_event |
| `on_sse_event` 抛异常 | log + 继续 dispatch 给下一个 strategy；不影响 SSE 流 |
| trader shutdown | `_stop_event.set()` → cancel SSE tasks → SseClient 退出 subscribe → httpx 长连接 close |

## 12. 测试

`pytest -x quant/tests/` 全过才能"声称完成"。

| 测试 | 覆盖 |
|---|---|
| `test_sse_client.py` | wire format 解析（含 comment 行/空行）；single-event yield；seq gap → 重连；25s 超时 → 重连；网络异常退避；ping 重置 timeout 但不累加 seq |
| `test_sse_subscriber.py` | trade event 写 trades 表；多 strategy dispatch 路由（只给关心该 market 的）；on_sse_event 抛异常不影响其他 strategy；market_status 走 dispatch + log；snapshot 只 log 不入表 |
| `test_store_trades.py` | `log_trade` 字段完整入库；重复 trade_id INSERT OR IGNORE 不抛；`bulk_insert_partial_trades` 批量；`recent_trades_observed` DESC ts |
| `test_rest_recent_trades.py` | `get_recent_trades` 解析返回；pydantic extra=allow 容忍未知字段 |

**Smoke 测试**（手工，跑 SSE 真打 prod）：

1. 改 config 开 dca outcome_id=1 (or 已知 active market)，enabled=true
2. `python -m thccb_quant --dry-run`
3. 看 `logs/system.jsonl` 应见 `sse_snapshot_bootstrapped` event
4. `sqlite3 state/quant.db "SELECT count(*) FROM partial_trades"` 应 ≥ 0（视后端是否有近 100 笔）
5. 等市场有人交易 → `sqlite3 state/quant.db "SELECT * FROM trades ORDER BY id DESC LIMIT 5"` 应见新行
6. `touch state/KILL` 优雅停机 → 看 SseSubscriber cancel 干净

## 13. 范围外 (YAGNI)

- Phase 2 的具体 SSE-driven 短线策略
- snapshot.outcomes 全字段持久化（snapshot 只做 bootstrap 锚点）
- 跨进程/跨机器事件转发
- Prometheus 指标 / Grafana 看板
- recent-trades 之外的更深历史回填
- SSE 事件流 dump 到独立 JSONL 文件（仍只用 `system.jsonl` 系统通道）
- 动态新 market 自动加订阅（启动时静态计算 market_ids，新市场上线需重启）

## 14. 风险

- **启动顺序变更**（§10）：从"_run_strategy 内 setup→tick"改成"trader
  统一先 setup → 起 SSE → 起 tick 循环"；这要求 DCA config 加 `market_id`
  必填字段（不再可选）；现有 `config.yaml`（如 dca_dryrun_outcome1）
  需手动补这个字段才能跑
- **trades 表无限增长**：每天可能上千行（活跃市场场景下），半年后
  GB 级；当前不做归档/分片；后续可加 cron 清理或压缩
- **SSE 鉴权可能未来变更**：后端如果加上 `Depends(current_active_user)`，
  匿名访问会突然失败；好在客户端始终带 Bearer，零迁移代价
- **partial_trades 与 trades 字段不一致**：复盘 SQL 要小心 UNION 时
  字段对齐；schema 注释明确这一点
- **on_sse_event 串行 dispatch**：单个策略卡住会阻塞后续策略；
  当前接受，未来加超时
- **`MessageBroker` 后端重启 = 客户端 seq 重置**：被 gap detection
  捕获后会触发重 bootstrap，无副作用，但短时间内可能出现"明明
  serv 重启 5 秒"的多次 gap recover log，不是 bug
