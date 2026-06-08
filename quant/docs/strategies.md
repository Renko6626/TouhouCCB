# 内置策略一览

当前 `quant/` 内置三个策略：**DCA**（定投）、**Grid**（网格）和
**VolatilityHarvest**（波动率收割）。前两个是 **polling 驱动**（按
`tick_interval_sec` 定时唤醒决策）；VolatilityHarvest 走 `Strategy.on_sse_event`
hook，是 **SSE 事件驱动**——每笔真实成交触发一次决策。

设计语义实现在 `quant/thccb_quant/strategy/{dca,grid,volharvest}.py`，
注册名是 `type` 字段（`config.yaml` 里用）。

---

## DCA — 定投

**目的**：每隔固定时间用固定金额买入某 outcome，直到达到总预算。
不依赖价格判断、不预测方向，纯被动建仓。

**注册类型**: `type: dca`

### Config 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | str | 实例名（多 DCA 共存时区分） |
| `enabled` | bool | 是否启用 |
| `market_id` | int | **必填**（SSE 路由需要） |
| `outcome_id` | int | 目标 outcome |
| `cny_per_buy` | float | 每次买入花多少 CNY |
| `interval_hours` | float | 两次买入最小间隔小时；填 `0` = 无间隔限制（纯靠预算挡） |
| `total_budget_cny` | float | 该策略生命周期总花销上限 |

### 触发逻辑（每 60 秒 tick 一次）

```
tick:
  if 距上次 buy 不足 interval_hours → skip "interval not reached"
  if 已花费 + cny_per_buy > total_budget_cny → skip "total budget exhausted"
  
  probe_quote = rest.quote(outcome_id, shares=1, side="buy")   # 拿当前 avg_price
  target_shares = cny_per_buy / probe_quote.avg_price          # 6 位精度
  
  try:
    broker.buy(target_shares)   # 实盘真下单 / dry-run 只写表
    spent += resp.cost          # 累加实际成本（含滑点）
    last_buy_ts = now
    log_decision(action="buy")
  except (RiskRejected, BusinessError, TransientError):
    log_decision(action="skip", reason=...)
```

### 重启行为

`setup()` 从 `orders` 表 replay 所有 status=success+side=buy 的行 →
累加进 `_spent`。所以重启后 `total_budget_cny` 累计是连续的（不是重置）。

### 示例配置

```yaml
- name: dca_market_1_outcome_yes
  type: dca
  enabled: true
  market_id: 1
  outcome_id: 1
  cny_per_buy: 5.0          # 每次 5 元
  interval_hours: 6         # 6 小时一次
  total_budget_cny: 200     # 总共 200 元，跑约 40 次
```

### 适用 / 不适用

- ✅ **适用**：你对某 outcome 有较强先验信念、想以平均成本建大仓位（如认为某事大概率成真，分散入场降低择时风险）
- ✅ **波动免疫**：不依赖价格判断，市场剧烈波动反而能买到更分散的成本
- ❌ **不收割散户**：纯被动接盘，散户的过激买卖不会成为你的利润源
- ❌ **不适合短期获利**：定投是长期建仓工具，几小时几天看不出效果
- ⚠️ **滑点感知**：高波动市场下，从 quote→buy 的几百毫秒间隔，实际成交价可能偏离 quote 几个百分点。`_spent` 用 `resp.cost`（真实成交）累加而非 `cny_per_buy`，所以预算限制是准的

---

## Grid — 网格

**目的**：在 `[price_low, price_high]` 区间内，价格上行卖、下行买，
赚价格在区间内来回的振幅差。本质是**均值回归赌注**。

**注册类型**: `type: grid`

### Config 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | str | 实例名 |
| `enabled` | bool | 是否启用 |
| `market_id` | int | 目标 market |
| `outcome_id` | int | 目标 outcome |
| `price_low` | float | 网格下界（如 0.30） |
| `price_high` | float | 网格上界（如 0.60，必须 > low） |
| `grid_count` | int | 网格段数 → 实际格点数 = grid_count + 1 |
| `shares_per_grid` | float | 每次买/卖的份数 |
| `tick_interval_sec` | int | 决策频率，默认 30 |

### 触发逻辑（"bin tracking" 语义）

价格被分成 `grid_count` 个 bin（即相邻格点之间的区间）。**只有当价格穿
到新的 bin 才触发动作**，避免价格不动反复刷单。

```
tick:
  market = rest.get_market(market_id)
  price = outcome.current_price
  bin = which bin does price fall into?           # None 表示出区间
  
  if bin is None → skip（不更新 _last_bin，再回到区间内按"首次"处理）
  if bin == _last_bin → skip "same bin"
  
  if first tick OR bin 下移（价格跌）:
    买入"当前价上方最近的未持仓格点"一份 shares_per_grid
  
  if bin 上移（价格涨）:
    卖出"当前价下方最近的已持仓格点"一份 shares_per_grid
  
  _last_bin = bin
```

每次 tick **最多一笔买 + 一笔卖**。

### 重启行为

`setup()` 从 `orders` 表 replay 所有 success 单 → 按时间正序重放，
根据 side=buy/sell 设置每个格点的 `_held` 状态。**注意**：`_last_bin`
不持久化，重启后按"首次 tick"处理（在当前价上方最近未持仓格点买）。

### 示例配置

```yaml
- name: grid_market_1_outcome_yes
  type: grid
  enabled: true
  market_id: 1
  outcome_id: 1
  price_low: 0.30
  price_high: 0.60
  grid_count: 6              # → 7 个格点 [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60]
  shares_per_grid: 2.0
  tick_interval_sec: 30
```

### 适用 / 不适用

- ✅ **适用**：相信某 outcome 的价格会在一个区间内来回震荡（如长期处于 0.4–0.6
  的不确定性中段事件）
- ❌ **不适合 thccb 当前市场**：用户实测 thccb LMSR 市场单笔可 ±10% 波动且
  常常单向跑（散户情绪驱动）。网格策略在剧烈单向波动场景下会**持续被套**——
  价格一路向上时只会一直卖出（持仓被清掉错过涨）；一路向下时一直买入（成本
  越买越高错过下一波底）。memory [project-market-volatility] 有详细分析。
- ⚠️ **格子大小要慎选**：spec §1.4 的 5% 步长在 ±10% 波动里**会被一笔吃穿多格**，
  实际只会触发一格。要么把 `grid_count` 调小（如 3 → 10% 步长），要么放弃
  网格换其他策略
- ⚠️ **`_last_bin` 不持久化**：重启后第一个 tick 会重新当作"首次进入"处理，
  可能在原本不该买的地方触发一次买入；如果担心，重启前先 `touch state/KILL`
  等优雅停（实际上无差别，因为持仓状态从 orders 表 replay 是正确的）

---

## VolatilityHarvest — 波动率收割

**目的**：吃散户情绪驱动的短期 mean reversion。维护 logit 空间的滑窗中位数
+ MAD，当当前价偏离中位数超过 `k_sigma × σ` 时反向调整持仓（涨太多卖、跌太多
买），让自然回归把持仓拉回底仓。

**注册类型**: `type: volharvest`

### 与 Grid 的区别

- Grid: 固定价位格点，价格穿过格点边界才动作；适合震荡区间已知的市场
- VolatilityHarvest: 动态 MAD 自适应阈值；适合 trend 缓慢漂移 + 短期 noise
  的市场（thccb 散户主导市场更适合）

### Config 字段

完整字段见 `quant/config.example.yaml`。

关键参数：

| 字段 | 含义 | 默认 |
|---|---|---|
| `window_size` | 滑窗 N 笔 SSE trade event | 100 |
| `k_sigma` | 触发 = k × 1.4826 × MAD（≈ σ 倍数） | 2.0 |
| `scale_mad` | deadband 之外 tanh 的尺度 | 1.0 |
| `base_shares` | 目标底仓（inventory，非 alpha 判断） | 500 |
| `max_offset_shares` | 偏离底仓上下限 | 200 |
| `min_trade_shares` / `max_trade_shares` | 单笔下限/上限 | 5 / 20 |
| `bootstrap_interval_sec` | bootstrap 节流 | 30s |
| `bootstrap_skip_if_overpriced` | 价格偏高时暂停 bootstrap | true |
| `reconcile_interval_sec` | 周期校准真实持仓 | 300s |
| `trend_guard_events` | 连续 N 笔同向 → 暂停逆势加仓 | 5 |

### 触发逻辑（每个 SSE trade event）

```
SSE trade → push (ts, logit_price) 到滑窗 + push side 到 trend_window
↓
reconcile（每 reconcile_interval_sec 校准一次 _holding）
↓
若 _holding < base_shares → bootstrap mode：
  - 间隔/overpriced 检查 → 通过则买 min(base-holding, bootstrap_max_step)
  - 不进入主信号流程
否则进入主信号：
  - 窗口未热（< N/2）→ skip
  - MAD < min_mad_logit → skip
  - threshold = k_sigma * 1.4826 * MAD
  - excess = max(0, |deviation| - threshold)
  - target = excess==0 ? base : base - max_offset * tanh(sign(dev) * excess/(scale*MAD))
  - delta = target - _holding，clip 到 ±max_trade_shares，drop 若 < min_trade_shares
  - trend guard：连续 trend_guard_events 笔同向 → 拦逆势单
  - 下单
```

### PnL 拆分（重要）

`base_shares=500` 是**库存池不是 alpha 判断**。复盘时必须拆：

```text
total_pnl = base_pnl + offset_pnl
  base_pnl   = (当前价 - bootstrap 均价) × base_shares    # directional bet 损益
  offset_pnl = Σ(每次 offset 调整的回归收益)              # 真正的波动率 alpha
```

每条 `volharvest_trade` 日志带 `bootstrap_mode` 字段，可用 jq 区分。

### 适用 / 不适用

- ✅ **适用**：thccb 散户情绪主导 + 价格 = 缓慢 trend + 短噪声的市场
- ✅ **真实 SSE 事件驱动**：每笔成交都更新统计，响应快
- ✅ **deadband + tanh**：小偏离不动，大偏离平滑加仓，**无 76% 跳变**
- ✅ **保险丝**：trend guard 拦逆势单 + max_offset 仓位硬上限 + 周期 reconcile
  防内部状态漂移
- ⚠️ **trend 真转风险**：mean reversion 策略固有；max_offset 是损失上限
- ⚠️ **base_shares 是 directional bet**：PnL 必须拆 base_pnl + offset_pnl 看
- ⚠️ **窗口预热慢**：thccb 低流动性下首次满 N=100 可能要 1-2 小时
- ⚠️ **滑点吃利润**：±10% 波动市场实盘前 `risk.max_slippage_bps` 至少调到 800

### 上实盘流程

按 spec §9 三阶段：

1. **短跑 60s**：验启停不崩，看到 `sse_partial_trades_preloaded` + `stopped_clean`
2. **长跑 6h+**：验主信号，看到 ≥100 个 `volharvest_signal` event + 至少一次
   reconcile + 至少一次 trend guard 触发
3. **微调**：`risk.max_slippage_bps` 调到 800-1500；`base_shares` 起步先 200
   观察 1-2 天 PnL 拆分；`risk.daily_loss_cap_cny` 设到 `base × avg_price × 0.5`
   量级

### 复盘 SQL/jq 示例

```bash
# 所有"想下单但被拦"的情况
jq 'select(.event=="volharvest_signal" and .reason!="ok")' logs/system.jsonl

# 只看 offset 交易（非 bootstrap）
jq 'select(.event=="volharvest_trade" and .bootstrap_mode==false)' logs/system.jsonl

# trend guard 触发频率
jq 'select(.event=="volharvest_trend_guard_blocked")' logs/system.jsonl | wc -l

# 决策表完整复盘
sqlite3 quant/state/quant.db "SELECT ts, action, reason FROM decisions WHERE strategy LIKE 'volharvest%' ORDER BY id DESC LIMIT 50"
```

---

## 共同行为

### 错误处理（所有策略）

`tick()` 内调 `broker.buy/sell` 时捕获 **3 类**异常：

| 异常 | 含义 | 行为 |
|---|---|---|
| `RiskRejected` | 风控拒（超 cap / 冷却中 / kill switch / 滑点超限 / 5s 幂等冲突） | log skip + 等下次 tick |
| `BusinessError` | 后端 4xx（余额不足 / 份额不足 / 市场关闭等） | log skip + 等下次 tick |
| `TransientError` | 5xx 或网络超时（rest 已重试过 3 次仍失败） | log skip + 等下次 tick |
| `FatalAuthError` | refresh token 失效 | **不捕获**，冒泡到 trader 触发全局停机 |
| 其他 `Exception` | 策略代码本身 bug | trader 的 `_run_strategy_loop` 捕获 + log + 单 tick 跳过；策略实例不死 |

### 日志位置

每次决策（含 skip）都会写一行到 SQLite `decisions` 表，含
`strategy / outcome_id / action / reason / snapshot_json`。复盘：

```bash
sqlite3 quant/state/quant.db \
  "SELECT ts, action, reason, snapshot_json FROM decisions
   WHERE strategy='dca_market_1_outcome_yes' ORDER BY id DESC LIMIT 20"
```

每次成交（含 dryrun 和 failed）写一行到 `orders` 表，含 price/cost/status。

### 多策略共存

`config.yaml` 的 `strategies:` 是个列表，可以同时 enable 多个；每个
策略一个 asyncio task，**互不阻塞**，共享同一个 `Store` / `Broker` /
`RiskGuard`（即风控的"日累计 cap"是**所有策略汇总**的，不是单策略）。

---

## 写一个新策略

完整规范见 Claude Code 项目本地 skill：
**`.claude/skills/writing-quant-strategy/SKILL.md`**

让 Claude Code 帮你写时它会自动 invoke 这个 skill。手动开发时也建议读一遍。

### 6 步速查

1. `quant/thccb_quant/strategy/<name>.py` — 继承 `Strategy`，`@register("<type>")`
2. `quant/thccb_quant/trader.py` 顶部加 `import thccb_quant.strategy.<name>`
   触发注册副作用（**不加 trader 拿不到这个 type**）
3. `quant/config.example.yaml` 加示例（必含 `market_id` 字段）
4. `quant/tests/test_strategy_<name>.py` — 三类测试：
   - 单元（信号触发逻辑）
   - 集成（用真 `SseSubscriber` + mock `SseClient` 验证 dispatch 路由）
   - replay（重启从 orders 表恢复内部状态）
5. `pytest -x` 全过
6. dry-run 30s+ smoke 真实环境，看 `decisions` 表 + `system.jsonl`

### 三条必须遵守的硬规则（skill 详述）

- `setup()` 第一行 `self.market_id = self._market_id` — SseSubscriber 路由依赖
- 只 catch `RiskRejected / BusinessError / TransientError` 三类 —
  catch 宽泛 `Exception` 会吞 `FatalAuthError` 让 trader 应停未停
- 下单永远走 `ctx.broker.buy/sell`，不要直接调 `ctx.rest.buy/sell`
  （会绕开风控/幂等/落账）

### 上实盘前

- dry-run 至少 ≥24 小时观察 `decisions` 表是否符合预期
- 用极小 `single_order_cap_cny`（1 元）+ 极小总预算跑一段，对账无误再放大
- 检查 `max_slippage_bps` 是否合理：spec 默认 300 (3%) 在 ±10% 波动市场会大量拒单，
  实盘前考虑放宽到 800-1500
