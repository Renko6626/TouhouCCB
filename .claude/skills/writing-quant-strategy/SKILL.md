---
name: writing-quant-strategy
description: Use when adding a new strategy class to the thccb-quant trading bot (quant/thccb_quant/strategy/), or when extending existing strategies with new signal sources or trading logic
---

# Writing a quant strategy for thccb-quant

## Overview

thccb-quant 是 `/data/sunyunbo/www/TouhouCCB/quant/` 下的实盘 LMSR 量化交易脚本。
新策略 = 继承 `Strategy` ABC，可选 `tick()`（polling）和 `on_sse_event()`
（实时事件）两个输入通道。

**强约束**：实盘真账号下单 + 高波动市场（单笔可 ±10%）+ 必须能干净 dry-run。

## 6 步开发流程

1. `quant/thccb_quant/strategy/<name>.py` — 写策略类，`@register("<type>")`
2. `quant/thccb_quant/trader.py` 顶部 imports 区加 `import thccb_quant.strategy.<name>`
   触发注册副作用（**不加 trader 永远拿不到这个 type**）
3. `quant/config.example.yaml` 加示例条目（必含 `market_id` 字段）
4. `quant/tests/test_strategy_<name>.py` — 至少 3 类测试：单元 + 集成 dispatch + replay
5. `cd quant && source .venv/bin/activate && pytest -x` 必过
6. **dry-run 至少 30s smoke 真实环境**：改 config enable 新策略 → `python -m thccb_quant --dry-run`
   → KILL → 看 `decisions` / `orders` 表 + `system.jsonl` 日志

## 必读参考

- 现有策略：`quant/thccb_quant/strategy/{dca,grid}.py`
- 测试模板：`quant/tests/test_strategy_{dca,grid}.py`
- 策略综述：`quant/docs/strategies.md`
- spec：`docs/superpowers/specs/2026-05-17-quant-trader-design.md` §6
- SSE spec：`docs/superpowers/specs/2026-05-17-sse-pipeline-design.md` §6

## 数据输入 — StrategyContext

策略通过 `setup(ctx: StrategyContext)` 拿到这些通道：

| 字段 | 类型 | 用法 | 注意 |
|---|---|---|---|
| `ctx.rest` | `RestClient` | 拿市场/账号数据 | 限速 8 r/s；4xx → BusinessError，5xx → TransientError，已重试 3 次 |
| `ctx.broker` | `Broker` | 下单（已内含 risk + 5s 幂等 + 落账） | 永远 catch `RiskRejected / BusinessError / TransientError` 三类 |
| `ctx.store` | `Store` | 读 trades/orders/partial_trades，写 decisions | log_order 是 broker 写的，策略别自己写 |
| `ctx.logger` | `structlog.BoundLogger` | 结构化日志 | 已绑 `strategy=<name>`；自己加 `outcome_id=`/`price=`/`delta_bps=` 等 |
| `ctx.config` | `dict` | 合并后的 `risk` + 该策略 config | 读 `max_slippage_bps` 用 `ctx.config["max_slippage_bps"]` 而不是策略自己的 config |

### ctx.rest 方法清单

| 方法 | 返回 | 用途 |
|---|---|---|
| `quote(outcome_id, shares, side)` | `QuoteResponse` (avg_price/gross/fee/net/after_prices) | 探价 |
| `buy/sell(outcome_id, shares, max_slippage_bps)` | `OrderResponse` | **别直接调**，走 `ctx.broker` |
| `get_market(market_id)` | `MarketDetail` (含 outcomes + 现价) | polling 现价 |
| `list_markets()` | `list[MarketSummary]` | |
| `get_holdings()` | `list[HoldingRead]` | 拿真实持仓（**含手动 UI 交易！**比 orders 表 replay 更可靠） |
| `get_user_summary()` | `UserSummary` (cash/debt/net_worth) | |
| `get_recent_trades(limit)` | `list[PartialTrade]` | 启动时已被 SseSubscriber 预加载到 partial_trades 表 |

### ctx.store 读方法（写不归你）

```python
await ctx.store.recent_orders(strategy=self.name, limit=10000)  # replay 自己历史
await ctx.store.recent_trades_observed(market_id=X, limit=50)   # SSE 收到的全市场成交（含散户）
await ctx.store.get_daily_stats(date)                            # 日累计（broker 维护）
```

`partial_trades` 表（启动 preload 的历史）现在没有 store 方法封装，直接 SQL：
```python
cur = await ctx.store._conn.execute(
    "SELECT * FROM partial_trades WHERE market_id=? ORDER BY ts DESC LIMIT ?", (mid, n)
)
```

## SSE 事件入口

```python
async def on_sse_event(self, event: SseEvent) -> None:
    if event.type != "trade":
        return
    trade = event.data["trade"]
    if trade["outcome_id"] != self._outcome_id:  # 只看自己关心的 outcome
        return
    # 字段：trade.id / type ("BUY"/"SELL") / shares / price / gross / fee /
    #       username / post_market_price / market_prices_post (list) / timestamp (ISO8601 UTC)
```

**事件 dispatch 由 SseSubscriber 按 `self.market_id` 路由**，所以
`market_id` **必须**在 `setup()` 里设置：

```python
async def setup(self, ctx: StrategyContext) -> None:
    self.market_id = self._market_id  # 否则 SseSubscriber 拿不到，事件永不路由过来
    self._ctx = ctx
    ...
```

## 数据输出契约

### log_decision schema

每次"想做点什么"（包括"想了但没做"）都该写一行：

```python
await ctx.store.log_decision(
    strategy=self.name,
    outcome_id=<int>,
    action="buy" | "sell" | "skip",
    reason="<短理由，jq 能 grep>",
    snapshot={"price": "...", "delta_bps": "...", ...},  # 任意 JSON-serializable
)
```

### structlog 事件（也要发）

`broker` 已经 emit `order_success/failed/dryrun`；策略层应该 emit 信号事件，
让人能 `jq 'select(.event=="momentum_signal_up")'` 复盘：

```python
ctx.logger.info("momentum_signal_up", outcome_id=..., delta_bps=..., price=str(price))
ctx.logger.warning("momentum_no_position_to_sell", outcome_id=..., held=str(self._held))
```

**只写 decisions 不写 structlog = 复盘要 sqlite3 + jq 双开**，麻烦。

### 下单 — 永远走 ctx.broker

```python
try:
    resp = await ctx.broker.buy(
        strategy=self.name,
        outcome_id=self._outcome_id,
        shares=Decimal("..."),
        max_slippage_bps=int(ctx.config["max_slippage_bps"]),  # 不是 .get default！
    )
except (RiskRejected, BusinessError, TransientError) as e:
    await ctx.store.log_decision(action="skip", reason=f"buy failed: {e}", ...)
    return
# 成功路径：broker 已经写 orders 表 + add_turnover + add_pnl + mark_order
# 策略自己只需更新内部状态（如 _held_shares）+ log_decision
```

**绝不**：
- catch 宽泛 `Exception`（会吞 FatalAuthError 导致 trader 应停未停）
- 自己直接调 `ctx.rest.buy/sell`（绕开风控 + 幂等 + 落账）
- 加 `accept_any_slippage=True`（spec 红线）

## 测试要求

三个层面，全在 `quant/tests/test_strategy_<name>.py`：

### 1. 单元（信号触发逻辑）

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from thccb_quant.state.store import Store
from thccb_quant.strategy.<name> import <Class>

@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "t.db")
    yield s
    await s.close()

async def _make_ctx(store):
    rest = MagicMock(); broker = MagicMock()
    broker.buy = AsyncMock(return_value=MagicMock(
        shares=Decimal("1"), cost=Decimal("0.5"), new_cash=Decimal("499.5")
    ))
    return StrategyContext(
        rest=rest, broker=broker, store=store,
        logger=structlog.get_logger("test"),
        config={"max_slippage_bps": 300},  # ctx.config 必须含 max_slippage_bps
    )

async def test_<your-trigger>(store):
    s = <Class>(name="t", config={...})
    ctx = await _make_ctx(store)
    await s.setup(ctx)
    # 直接调你的入口方法
    await s.on_sse_event(_fake_trade_event(...))
    assert ctx.broker.buy.call_count == 1
```

### 2. 集成（SseSubscriber dispatch 路由）

确保 SseSubscriber 真的能把事件喂到你的策略：

```python
from thccb_quant.client.sse_subscriber import SseSubscriber

async def test_subscriber_dispatches_to_my_strategy(store):
    s = <Class>(name="t", config={"market_id": 1, ...})
    ctx = await _make_ctx(store)
    await s.setup(ctx)  # 让 market_id 落定

    sse = MagicMock()
    async def fake_subscribe(market_id):
        yield _fake_trade_event(market_id=1, outcome_id=1)
    sse.subscribe = fake_subscribe

    rest = MagicMock()
    rest.get_recent_trades = AsyncMock(return_value=[])
    sub = SseSubscriber(rest=rest, store=store, sse_client=sse,
                       strategies=[s], market_ids={1},
                       logger=structlog.get_logger("test"))
    await asyncio.wait_for(sub.run(), timeout=2.0)
    # 验证策略收到了事件（看 ctx.broker.buy 被调 / decisions 表新增 / 内部状态变化）
```

### 3. Replay（重启恢复内部状态）

如果策略有持久内部状态（如累计花费、持仓估算），必须能从 `orders` 表 replay：

```python
async def test_replay_recovers_state(store):
    # 先写两笔历史 orders
    await store.log_order(strategy="t", outcome_id=1, side="buy",
                          shares=Decimal("3"), price=Decimal("0.5"),
                          cost=Decimal("1.5"), status="success")
    # 重建策略，setup 后应恢复
    s = <Class>(name="t", config={...})
    ctx = await _make_ctx(store)
    await s.setup(ctx)
    assert s._held_shares == Decimal("3")  # 或你的状态变量
```

**Replay 的坑**：`store.recent_orders` 只读自己策略名下的成交。如果用户在 thccb 站
**手动 UI 交易**了同一个 outcome，策略会**少算持仓**。两个解法：

- (a) **不要混着用**——策略跑期间别手动交易该 outcome（最简单）
- (b) `setup()` 里加 `holdings = await ctx.rest.get_holdings()` 用真实余额 bootstrap
  内部状态（最准确但要写更多代码）

文档化你选了哪条。

## 高波动市场的坑

memory `project-market-volatility` 详细描述。两个对策略写法的直接影响：

1. **`max_slippage_bps: 300`（默认 3%）在 ±10% 波动市场会拒掉很多单**。如果你的
   策略对成交价不太敏感（如 DCA），实盘前要把 config 的 `risk.max_slippage_bps`
   放宽到 800-1500。在 spec 范围内调整 config，不要在策略里 hack。

2. **SSE 事件之间可能价格已变 5%+**。`on_sse_event` 拿到的 `trade.post_market_price`
   是**那笔成交完之后的**价；你做信号判断和下单之间的几百毫秒，市场可能又动了
   几次。所以下单时 broker 的 quote 价跟你信号触发时的价大概率不一样——这是预期，
   不是 bug。

## SSE 事件细节

`event.data["trade"]["timestamp"]` 是 server ISO8601 UTC 字符串。解析：

```python
from datetime import datetime, timezone
dt = datetime.fromisoformat(ts_str.rstrip("Z"))
if dt.tzinfo is None:
    dt = dt.replace(tzinfo=timezone.utc)
epoch = dt.timestamp()
```

**解析失败**：log warning + skip 该事件。**不要** fallback 到本地 `time.time()`
混进窗口（local clock vs server clock 不同步会让滑窗算法出 bug）。

## 完成准入清单

不在 production 跑前必过：

- [ ] `pytest -x quant/tests/` 全过
- [ ] 单元 + 集成 dispatch + replay 三类测试都有
- [ ] `python -c "from thccb_quant.trader import main_async"` 不报错
- [ ] config.example.yaml 加示例（含 `market_id`）
- [ ] trader.py 顶部加了 `import thccb_quant.strategy.<name>`
- [ ] 改 config enable 新策略 → dry-run 30s+ → 看 decisions 表确认决策符合预期
- [ ] dry-run 期间没有 ERROR 日志（除已知/预期的）
- [ ] 文档（哪怕 README 一段）说清"这个策略假设什么市场行为、什么时候会失效"

## Common Mistakes

| 错误 | 后果 | 修法 |
|---|---|---|
| 忘 `@register("<type>")` | trader `get_strategy_class` 抛 KeyError，"unknown strategy type" | 装饰类 |
| 忘 trader.py 顶部 import | 同上（注册副作用没触发） | 加 `import thccb_quant.strategy.<name>` |
| 忘 `self.market_id = ...` in setup | SseSubscriber 用 `s.market_id` 路由，永远是 None → 事件路由不到 | setup 第一行设 |
| `__init__` 用 `config.get("market_id")` 默认 None | 同上 + 后续 int() 转换抛错 | 用 `int(config["market_id"])`，缺字段就 KeyError 让 trader 友好报错（已有 try/except） |
| catch `except Exception` | 吞 FatalAuthError 让 trader 应停未停 | 只 catch `RiskRejected, BusinessError, TransientError` |
| 自己调 `ctx.rest.buy` 绕过 broker | 不过风控、不写 orders、日累计不累加 | 永远走 `ctx.broker.buy/sell` |
| 测试只调 `await s.on_sse_event(ev)` 没过 SseSubscriber | dispatch 路由 bug 测不出（市场号写错、注册漏） | 加集成测试用 mock SseClient + 真 SseSubscriber |
| log_decision 不写 snapshot 字段 | 复盘时只看到 action 不知当时 price/delta | snapshot 里塞触发条件相关数值 |
| SSE timestamp 解析失败 fallback 用 `time.time()` | 服务端时间 vs 本地时间不同步，滑窗错乱 | log warning + skip 事件 |
| 默认 `max_slippage_bps: 300` 实盘 | 高波动市场大量被拒 | 实盘前把 config.risk.max_slippage_bps 放到 800-1500 |
| 策略跑期间用 thccb UI 手动交易同 outcome | replay 持仓少算 | 不混用，或 setup 里 bootstrap from rest.get_holdings |

## Red Flags — 停下来重看

- "我先 catch Exception，反正知道哪几类异常" → 不行，FatalAuthError 必须冒泡
- "测试 mock 一下 broker 就够了" → 不够，还要测 SseSubscriber dispatch
- "默认 max_slippage_bps=300 跑实盘看看" → 这个市场会大量拒单，先调
- "register 装饰器加了就行，trader.py 不用动" → 不行，trader 不 import 你的模块 = 装饰器副作用不触发
- "用 time.time() 给 SSE 窗口" → server 时钟和本地不一定同步，要用 server 给的 timestamp
