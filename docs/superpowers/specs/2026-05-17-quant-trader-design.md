# Quant Trader 量化交易脚本设计

**日期**: 2026-05-17
**作者**: renko6626
**状态**: 设计稿（待实现）

## 1. 背景与目标

学习市场算法（LMSR AMM、动量、均值回归、网格、DCA 等）的练手项目。
在主仓 `TouhouCCB/` 下新增独立子目录 `quant/`，写一个**调本站 REST/SSE
API 的真账号实盘量化交易脚本**。

**非目标**：

- 不做高频套利、不与生产前端竞争用户体验
- 不做回测引擎、不做 Web UI、不做事件总线（YAGNI）
- 不做多账号、不做收益最大化（学习优先于赚钱）

**关键约束**：

- **实盘**：调用所有者本人在生产站的账号下单，每笔都是真金白银（小额）
- **公用开发服务器无 sudo**：部署只能纯 user space
- **后端零侵入**：不改任何后端代码、`docker-compose.yml`、`deploy/`、CI

## 2. 整体架构

单进程 asyncio 程序，模块化四层：

```
┌──────────────────────────────────────────────────┐
│  trader.py（入口 + 调度 + kill switch 监视）        │
└────────┬────────────────────────────────────────┘
         │
   ┌─────┴─────┐  注入  ┌──────────────────────────┐
   │ Strategy   │◄──────│ StrategyContext           │
   │ (grid/dca) │       │   rest / broker / store   │
   └─────┬─────┘       │   lmsr / logger / config  │
         │             └──────────────────────────┘
         ▼
   ┌──────────┐   ┌──────────┐   ┌─────────┐
   │ Broker    │──►│ RestClient│──►│后端     │
   │ + Risk    │   │ SseClient │   │API     │
   └─────┬────┘   └──────────┘   └─────────┘
         │
         ▼
   ┌──────────┐
   │  Store    │   aiosqlite: orders / decisions / daily_stats
   └──────────┘
```

四层职责：

- **Client**：REST + SSE + token 自动续期，限速节流
- **Broker**：下单网关，下单前强制走 Risk 校验
- **Strategy**：业务逻辑，可插拔，第一批内置 Grid + DCA
- **State**：SQLite 三张表持久化订单/决策/日累计

## 3. 目录结构

```
quant/                          # 主仓子目录（独立 venv）
├── README.md                   # 怎么跑、风控参数、kill switch 说明
├── run.sh                      # 看门狗包装（崩了重拉）
├── pyproject.toml              # 独立 venv（uv 管）
├── config.example.yaml         # 配置模板（入 git）
├── config.yaml                 # 真实配置（gitignore）
├── .env.example                # token 模板（入 git）
├── .env                        # 真实 token（gitignore）
├── trader.py                   # 入口：python -m thccb_quant.trader
├── thccb_quant/                # 包源码
│   ├── __init__.py
│   ├── client/
│   │   ├── auth.py             # TokenManager: 加载/续期/落盘
│   │   ├── rest.py             # httpx.AsyncClient 封装 + 限速节流
│   │   └── sse.py              # SSE 订阅 + 自动重连（预留，起步不开）
│   ├── broker/
│   │   ├── base.py             # Broker ABC
│   │   ├── live.py             # LiveBroker 真实下单
│   │   ├── dryrun.py           # DryRunBroker 演练模式
│   │   └── risk.py             # 风控 + kill switch
│   ├── strategy/
│   │   ├── base.py             # Strategy ABC + StrategyContext
│   │   ├── registry.py         # STRATEGY_REGISTRY
│   │   ├── grid.py             # 网格策略
│   │   └── dca.py              # DCA 定投
│   ├── state/
│   │   ├── store.py            # aiosqlite 包装
│   │   └── schema.sql          # orders / decisions / daily_stats
│   ├── lmsr.py                 # 本地复现报价（策略本地预判用）
│   ├── errors.py               # 异常分类
│   └── logging_setup.py        # structlog JSON 日志
├── state/                      # 运行时 SQLite + KILL 文件（gitignore）
├── logs/                       # 结构化日志（gitignore）
└── tests/
    ├── test_lmsr.py            # 与后端 lmsr.py 对照
    ├── test_token.py           # token 续期 + 并发锁
    ├── test_risk.py            # 风控全分支
    ├── test_broker.py          # 用 respx mock httpx
    ├── test_strategy_grid.py   # FakeBroker 驱动
    └── test_strategy_dca.py
```

主仓 `.gitignore` 追加：

```gitignore
# quant trader
quant/.env
quant/config.yaml
quant/state/
quant/logs/
quant/.venv/
quant/__pycache__/
quant/**/__pycache__/
quant/*.db*
```

## 4. Client 层

### 4.1 TokenManager (`client/auth.py`)

**初始化（一次性手工）**：

1. 浏览器登 thccb.com，F12 → Network → 复制任意请求的
   `Authorization: Bearer <access>`
2. 同样从响应或 cookie 拿 refresh token
3. 填到 `quant/.env`：

   ```env
   THCCB_BASE_URL=http://127.0.0.1:8004
   THCCB_ACCESS_TOKEN=eyJ...
   THCCB_REFRESH_TOKEN=eyJ...
   ```

**自动续期**：

```python
class TokenManager:
    def __init__(self, env_path: Path):
        self._env_path = env_path   # 显式绝对路径，不依赖 cwd
        ...

    async def get_valid_access(self) -> str:
        if self._exp - time.time() < 300:  # 剩 < 5 min
            async with self._refresh_lock:
                if self._exp - time.time() < 300:
                    await self._refresh()
        return self._access

    async def _refresh(self):
        resp = await self._raw_client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": self._refresh},
        )
        if resp.status_code != 200:
            raise FatalAuthError("refresh failed, manual re-login needed")
        data = resp.json()
        self._access = data["access_token"]
        self._exp = jwt_decode_exp(self._access)
        dotenv.set_key(str(self._env_path), "THCCB_ACCESS_TOKEN", self._access)
```

`env_path` 由 `trader.py` 入口解析为 `Path(__file__).parent.parent / ".env"`
（即 `quant/.env` 的绝对路径），避免对 cwd 的隐式依赖。

- Access token 寿命 60 min（后端 `ACCESS_TOKEN_EXPIRE_MINUTES`），每请求前
  检查剩余时间
- Refresh token 寿命 7 天，到期前 1 天打 ERROR 日志要求手工重登；到期则
  抛 `FatalAuthError` → 全局停机
- 写回 `.env` 用 `python-dotenv.set_key`（原子写）

### 4.2 RestClient (`client/rest.py`)

封装这些端点：

- `quote(outcome_id, shares, side)` → `QuoteResponse`
- `buy(outcome_id, shares, max_slippage_bps)` → `OrderResponse`
- `sell(outcome_id, shares, max_slippage_bps)` → `OrderResponse`
- `list_markets(...)` → `list[MarketSummary]`
- `get_market(market_id)` → `MarketDetail`
- `get_positions()` → `PositionsResponse`
- `get_trades(market_id, limit)` → `list[Trade]`
- `get_me()` → `UserInfo`

**自限速**：内置 token bucket，默认 8 r/s（留 buffer 给后端 10 r/s 上限）。

**错误处理**：

- 429/5xx → 指数退避重试最多 3 次，仍失败抛 `TransientError`
- 4xx 业务错 → 抛 `BusinessError(status, message)`
- 网络超时（默认 10s）→ `TransientError`

**日志**：每请求记 `endpoint` / `latency_ms` / `status`，不打 token。

### 4.3 SseClient (`client/sse.py`)

`subscribe(market_id) -> AsyncIterator[Event]`：

- 自动重连（带 `last_seq` 锚点重续）
- 25s 心跳 ping 监测，连续 2 个 ping 间隔无事件视为僵尸
- 后端 1 小时强断，到点前主动重连

**起步不启用**：Grid/DCA 都是低频策略只用 REST polling 就够；SSE 保留
strategy hook (`on_sse_event`)，等后续加动量策略再开订阅。

## 5. Broker + Risk 层

### 5.1 风控参数（`config.yaml` 的 `risk:` 段）

| 参数 | 默认 | 含义 |
|---|---|---|
| `single_order_cap_cny` | 50 | 单笔最大成本 CNY |
| `daily_loss_cap_cny` | 100 | 当日累计净亏达此值，broker 转 read-only |
| `daily_turnover_cap_cny` | 2000 | 当日成交流水上限 |
| `max_slippage_bps` | 300 | 强制透传 server，永不开 `accept_any_slippage` |
| `min_seconds_between_orders` | 3 | 同 outcome 下单冷却 |

### 5.2 Risk 检查 (`broker/risk.py`)

下单前最后一道闸，逐项校验：

1. Kill switch 文件 `state/KILL` 存在 → 拒
2. 单笔成本 > `single_order_cap_cny` → 拒
3. 当日累计净亏 ≥ `daily_loss_cap_cny` → 拒，broker 整体转 read-only
4. 当日流水 + 本笔 > `daily_turnover_cap_cny` → 拒
5. 同 outcome 上次下单 < `min_seconds_between_orders` → 拒
6. `max_slippage_bps` 校验：策略传的若超 config 设的硬上限，拒（不允许
   策略个体放宽）

任何一项拒 → 抛 `RiskRejected(reason)`。

### 5.3 LiveBroker (`broker/live.py`)

```python
async def buy(self, outcome_id, shares, max_slippage_bps):
    quote = await self.rest.quote(outcome_id, shares, "buy")
    self.risk.check(quote, outcome_id, "buy", max_slippage_bps)
    # 幂等：5s 内同 strategy+outcome+side+shares 的 status='success' 或
    # 'dryrun' 记录 → 拒（failed 单未真下出去可重试）
    if await self.store.has_recent_duplicate(
        strategy, outcome_id, "buy", shares,
        within_sec=5, statuses=("success", "dryrun"),
    ):
        raise RiskRejected("duplicate within 5s")
    try:
        resp = await self.rest.buy(outcome_id, shares, max_slippage_bps)
        await self.store.log_order(status="success", ...)
        await self.store.add_turnover(resp.cost)
        return resp
    except BusinessError as e:
        await self.store.log_order(status="failed", error=str(e), ...)
        raise
```

### 5.4 DryRunBroker (`broker/dryrun.py`)

签名同 LiveBroker，但最后一步不调 `rest.buy/sell`，只往 `orders` 表写
状态 `dryrun`。

**Dry-run 是命令行 flag，与 watchdog 解耦**：

- `python -m thccb_quant.trader --dry-run` 全局替换为 `DryRunBroker`
- watchdog（`run.sh`）永远跑实盘，不走 dry-run
- **新策略验证流程**：临时停 watchdog (`touch state/KILL`) →
  前台 `python -m thccb_quant.trader --dry-run` 跑 24h → 看
  `logs/decisions.jsonl` 和 `orders.jsonl` 的 dryrun 行 → OK 后把策略
  加进 `config.yaml` → `rm state/KILL` 重启 watchdog

### 5.5 Kill Switch

两种触发方式：

1. **文件触发**：`touch quant/state/KILL`，主循环每 5s 检查
2. **风控自动**：
   - 日亏达 cap → broker 转 read-only，CRITICAL 日志，**不退出进程**（让
     用户看清状态手工处理）
   - 连续 5 次下单 4xx 错误 → 同上

**重启行为**：

- 启动时检测 `state/KILL` 存在 → 拒绝启动，要求手工 `rm state/KILL`
- 启动时读 `daily_stats` 当日累计 → 继续累加，不重置

## 6. Strategy 层

### 6.1 Strategy ABC (`strategy/base.py`)

```python
@dataclass
class StrategyContext:
    rest: RestClient
    broker: Broker
    store: Store
    lmsr: LmsrCalc
    logger: structlog.BoundLogger
    config: dict  # 该策略实例的 yaml 段

class Strategy(ABC):
    name: str
    tick_interval_sec: int

    @abstractmethod
    async def setup(self, ctx: StrategyContext): ...

    @abstractmethod
    async def tick(self): ...

    async def on_sse_event(self, event): ...   # 默认 no-op

    async def teardown(self): ...
```

### 6.2 内置策略

**GridStrategy** (`strategy/grid.py`)：在某 outcome 的
`[price_low, price_high]` 区间分 N 格，价格穿过格点时低买高卖；用本地
LMSR 预判触发。

```yaml
- name: grid_market_42_outcomeA
  type: grid
  enabled: true
  market_id: 42
  outcome_id: 123
  price_low: 0.30
  price_high: 0.60
  grid_count: 6
  shares_per_grid: 2.0
  tick_interval_sec: 30
```

**DcaStrategy** (`strategy/dca.py`)：每 `interval_hours` 固定金额买入某
outcome，可设总预算上限。

```yaml
- name: dca_market_99_yes
  type: dca
  enabled: true
  outcome_id: 200
  cny_per_buy: 5.0
  interval_hours: 6
  total_budget_cny: 200
```

### 6.3 LMSR 本地复现 (`lmsr.py`)

公式：

```
C(q) = b · ln(Σ exp(qi/b))     # 总成本
P_i  = exp(qi/b) / Σ exp(qj/b) # 选项 i 的瞬时价格
```

Python 实现需用 `max-shift` 防 exp 溢出。`tests/test_lmsr.py` 用后端
`backend/app/services/lmsr.py` 的几个已知 case 做对照，差异 < 1e-8 才过。
**这是最重要的测试**——本地公式偏了所有基于本地预判的策略都跑偏。

## 7. State 层

SQLite 三张表（原生 SQL，不上 ORM）：

```sql
CREATE TABLE orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,              -- ISO8601 UTC
  strategy TEXT NOT NULL,
  outcome_id INTEGER NOT NULL,
  side TEXT NOT NULL,            -- 'buy' / 'sell'
  shares TEXT NOT NULL,          -- Decimal 6 位字符串
  price TEXT,                    -- Decimal 8 位字符串
  cost TEXT,                     -- Decimal 6 位字符串
  status TEXT NOT NULL,          -- 'success' / 'failed' / 'dryrun'
  error TEXT
);
CREATE INDEX idx_orders_dedup
  ON orders (strategy, outcome_id, side, shares, ts);

CREATE TABLE decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  strategy TEXT NOT NULL,
  outcome_id INTEGER,
  action TEXT NOT NULL,          -- 'buy' / 'sell' / 'skip'
  reason TEXT,                   -- 'below_threshold' / 'risk_rejected' / ...
  snapshot_json TEXT             -- 触发时的市场/持仓快照
);

CREATE TABLE daily_stats (
  date TEXT PRIMARY KEY,         -- 'YYYY-MM-DD' UTC
  gross_turnover TEXT NOT NULL DEFAULT '0',
  net_pnl TEXT NOT NULL DEFAULT '0'
);
```

## 8. 错误处理

```python
class QuantError(Exception): ...
class FatalAuthError(QuantError): ...    # refresh 失败 → 全局停机
class RiskRejected(QuantError): ...      # 风控拒单 → 策略 catch 跳过本轮
class TransientError(QuantError): ...    # 5xx/超时 → 已重试过，跳过本轮
class BusinessError(QuantError): ...     # 4xx 业务错（余额不足等）
class StrategyError(QuantError): ...     # 策略自身 bug → 单策略停其他继续
```

**策略隔离**：每个 strategy 一个独立 asyncio task，单个 strategy 抛
`StrategyError` 不影响其他；`FatalAuthError` 才全局停机。

## 9. 日志（structlog → JSON Lines）

每条 log 带 `strategy` / `outcome_id` / `market_id` / `trace_id` 字段。

三个文件按天 rotate：

- `logs/orders.jsonl` —— 所有下单尝试（成功 + 失败）
- `logs/decisions.jsonl` —— 所有策略决策（含"未下单"）
- `logs/system.jsonl` —— token 续期、重连、kill switch、risk 告警

复盘示例：

```bash
jq 'select(.strategy=="grid_market_42_outcomeA" and .status=="success") | .cost' \
   logs/orders.jsonl
```

## 10. 测试

`pytest -x quant/tests/` 全过才算"声称完成"。

1. **`test_lmsr.py`** —— 与后端 `lmsr.py` 已知 case 对照，差异 < 1e-8
2. **`test_token.py`** —— mock httpx：
   - 剩余 > 5 min 不刷
   - 剩余 < 5 min 自动刷
   - 并发 10 请求只触发 1 次刷新（lock 生效）
   - refresh 返回 401 → 抛 `FatalAuthError`
3. **`test_risk.py`** —— 全分支：单笔超限、累计亏损达 cap、冷却期内、
   kill switch、滑点 bps 校验、边界值（cap / cap+1 分）
4. **`test_broker.py`** —— 用 `respx` mock httpx：quote 失败不调 buy、
   buy 200 必落 orders、buy 400 必落 orders（failed）、5s 幂等拒
5. **`test_strategy_grid.py` / `test_strategy_dca.py`** —— `FakeBroker`
   in-memory，喂构造价格序列断言下单时机和数量

**Smoke 测试**（手工，进 prod 前必跑）：

新建最小 config（单笔 cap 1 CNY、日累计 cap 5 CNY），跑 GridStrategy 在
低流动性小市场上：

1. 看是否真的下单成功
2. 立刻 `touch state/KILL` 看是否优雅停机
3. 检查 `logs/orders.jsonl` 有真实成交记录
4. 检查 thccb.com 持仓页有对应仓位
5. 手动平仓
6. 删 `state/KILL`、改回正常 config

## 11. 部署（无 sudo 公用机）

### 11.1 起步

```bash
cd quant/
uv venv && source .venv/bin/activate
uv pip install -e .
cp config.example.yaml config.yaml && $EDITOR config.yaml
cp .env.example .env && $EDITOR .env
pytest -x
python -m thccb_quant.trader --dry-run     # 演练
python -m thccb_quant.trader               # 实盘（含 run.sh 看门狗时不需要直接跑）
```

### 11.2 长跑：tmux + 看门狗 + user cron

`quant/run.sh`：

```bash
#!/usr/bin/env bash
set -u
cd "$(dirname "$0")"
source .venv/bin/activate

while true; do
  echo "[$(date '+%F %T')] starting trader"
  python -m thccb_quant.trader
  EXIT=$?
  if [ $EXIT -eq 0 ]; then
    echo "[$(date '+%F %T')] clean exit, watchdog quitting"
    break
  fi
  echo "[$(date '+%F %T')] crashed exit=$EXIT, restart in 30s"
  sleep 30
done
```

**启动**：

```bash
tmux new -d -s quant 'bash /data/sunyunbo/www/TouhouCCB/quant/run.sh'
```

**查看 / 进入**：

```bash
tmux attach -t quant      # 看实时输出，Ctrl-b d 退出不影响进程
tmux ls
```

**停**：

```bash
touch /data/sunyunbo/www/TouhouCCB/quant/state/KILL   # 优雅停
# 硬停：
tmux kill-session -t quant
```

**开机自启**（用户 crontab，无需 sudo）：

```cron
@reboot tmux new -d -s quant 'bash /data/sunyunbo/www/TouhouCCB/quant/run.sh' >> /data/sunyunbo/www/TouhouCCB/quant/logs/cron.log 2>&1
```

### 11.3 公用机额外注意

- 单进程 asyncio，内存 < 100MB，CPU 几乎为 0（REST polling 每 30s）
- 不开监听端口（纯出站客户端）
- 文件全在 `/data/sunyunbo/...`，权限 700
- 用 `setproctitle` 把进程名改 `thccb-quant`，`ps aux` 一眼能认

### 11.4 后端零侵入

- ❌ 不改后端任何代码 / `docker-compose.yml` / `deploy/` / `.github/workflows/`
- ❌ 不动数据库（自己 SQLite）/ 不动 `.env`
- ✅ 只新增 `quant/` 目录 + 主仓 `.gitignore` 追加 quant 忽略规则
- ✅ 只动你个人 crontab 一行 + 你个人 tmux session

## 12. 范围外（YAGNI）

- 回测引擎、模拟撮合
- Web UI / Prometheus 指标
- 多账号、多账号汇总报表
- 自动调参 / ML 策略
- 事件总线、消息队列
- 条件单 / 止盈止损单（站点本身不支持）

## 13. 风险清单

- **资金**：实盘真亏。`single_order_cap_cny=50` + `daily_loss_cap_cny=100`
  作为硬上限，配合 dry-run 24h 准入流程降风险
- **Token 泄露**：`.env` 严格 gitignore + 文件权限 600；公用机其他用户
  有 root 时仍能看到，无法消除（这是公用机固有问题）
- **重启丢状态**：用 SQLite 持久化 `daily_stats`，重启不重置当日累计
- **LMSR 公式偏**：`test_lmsr.py` 对照后端是硬约束
- **后端 API 变更**：调用方需要在 client 层用 pydantic 模型解响应，schema
  变了 fail-fast 抛 `BusinessError`，而不是默默吃错数据
