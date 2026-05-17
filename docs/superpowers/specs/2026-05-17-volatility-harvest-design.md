# VolatilityHarvest 策略设计

**日期**: 2026-05-17
**作者**: renko6626
**状态**: 设计稿（待实现）
**前置**: `docs/superpowers/specs/2026-05-17-quant-trader-design.md`（量化主 spec）+
`docs/superpowers/specs/2026-05-17-sse-pipeline-design.md`（SSE 管道）
**Skill**: `.claude/skills/writing-quant-strategy/SKILL.md`（写策略硬规则）

## 1. 目标与假设

**市场观察**（用户反馈 + memory `project-market-volatility`）：
- thccb LMSR 市场散户情绪主导
- 价格 ≈ 缓慢漂移 trend + 短期情绪 noise
- 单笔成交可让价格 ±10%（追涨杀跌 + 流动性低）

**策略目标**：吃 noise 部分的均值回归 alpha。
**非目标**：预测 trend 方向、做 directional bets、抓住信息驱动的真趋势。

**关键设计选择**：
- 价格空间用 **logit**（消除边界压缩 + 让信念变化对称）
- 均值估计用 **滑窗中位数**（鲁棒抗单笔极端冲击）
- 离散度用 **MAD**（与中位数配套）
- 仓位调整用 **tanh 饱和函数**（平滑、是颎动、明确上限）
- **双边震荡 + 自动 bootstrap 底仓**

## 2. 数据流

```
SSE trade event (每笔 outcome 相关成交)
     │
     ▼
取 trade.post_market_price (Decimal) → float
     │
     ▼
to_logit(p) = ln(p / (1-p))，p clamp 到 [0.001, 0.999]
     │
     ▼
window: deque[(ts, logit)] (maxlen=window_size) ← push 当前
trend_window: deque[side] (maxlen=trend_guard_events) ← push 当前 trade side
     │
     ▼
预热/可用性检查 (任一不过 → log skip 退出):
  - len(window) < window_size // 2   → skip "window_not_warm"
  - mad_logit < min_mad_logit         → skip "mad_too_small"
     │
     ▼
median_logit = median(logit for ts,logit in window)
mad_logit    = median(|logit - median_logit| for ts,logit in window)
robust_sigma = 1.4826 * mad_logit            # 让 k_sigma 直观地等价于 σ 倍数
threshold    = k_sigma * robust_sigma
deviation    = current_logit - median_logit
     │
     ▼
Bootstrap mode（holding < base_shares）:
  - 节流：now - last_bootstrap_ts < bootstrap_interval_sec → 跳过本轮
  - 价格保护：window 已热 且 deviation > 0 → skip "bootstrap_skip_overpriced"
  - 否则：delta_bootstrap = min(base_shares - _holding, bootstrap_max_step, max_trade_shares)
          → broker.buy(delta_bootstrap)
  - 不进入主信号流程（本 event 处理完）
     │
     ▼
Reconcile（周期）:
  若 now - last_reconcile_ts ≥ reconcile_interval_sec:
    actual = rest.get_holdings() 找本 outcome 的 amount
    若 |actual - _holding| > reconcile_tolerance:
      log warning "reconcile_drift_corrected" {actual, internal}
      _holding = actual
    last_reconcile_ts = now
     │
     ▼
主信号计算 (deadband + tanh):
  excess = max(0, |deviation| - threshold)
  
  若 excess == 0:
    target = base_shares          # deadband 内回归底仓（自然平掉之前的 offset）
  否则:
    u = sign(deviation) * (excess / (scale_mad * mad_logit))
    target = base_shares - max_offset_shares * tanh(u)
     │
     ▼
delta = target - _holding
若 |delta| < min_trade_shares → skip "delta_too_small"
若 |delta| > max_trade_shares → clip to ±max_trade_shares (log "delta_clipped")
     │
     ▼
Trend guard:
  若 trend_window 满 且 全部 same side:
    若 全 BUY (散户在追涨) 且 delta < 0 (我们想卖反向)
       → skip "trend_guard_up_blocks_sell"
    若 全 SELL (散户在杀跌) 且 delta > 0 (我们想买反向)
       → skip "trend_guard_down_blocks_buy"
  减仓 / 同向操作不受 trend guard 限制
     │
     ▼
若 delta > 0 → broker.buy( shares=delta )
若 delta < 0 → broker.sell(shares=|delta|)
     │
     ▼
成功路径: _holding += delta，emit volharvest_trade event
失败路径: log_decision skip + _holding 不变
```

## 3. 公式细节

### 3.1 logit 转换

```python
def to_logit(p: float) -> float:
    p = max(0.001, min(0.999, p))   # clamp 防边界爆炸
    return math.log(p / (1 - p))
```

### 3.2 仓位映射（deadband + tanh，**修订版**）

旧版 `target = base - max_offset * tanh(deviation / (k * mad))` 有一个**致命缺陷**：
deviation 刚等于 `k * mad`（恰好触发）时 `tanh(1) ≈ 0.762`，target 直接跳到
`base ± 0.762 * max_offset`——这不是平滑的 tanh 信号，而是"带 76% 跳变的阈值
触发"，会导致**刚跨阈值就下大单**。

新版引入 **deadband + 余值过 tanh**：

```python
threshold = k_sigma * 1.4826 * mad_logit   # 用 1.4826*MAD 等价 σ 让 k_sigma 直观
excess    = max(0.0, abs(deviation) - threshold)

if excess <= 0:
    target = base_shares                   # deadband 内回归底仓（自然平掉之前偏移）
else:
    u = math.copysign(excess / (scale_mad * mad_logit), deviation)
    target = base_shares - max_offset_shares * math.tanh(u)
```

数值特性（base=500, max_offset=200, k_sigma=2, scale_mad=1.0）：

| deviation | excess | u | target | delta from base |
|---|---|---|---|---|
| 0 | 0 | — | 500 | 0 |
| 0.5 * threshold | 0 | — | 500 | 0（deadband 内） |
| 1.0 * threshold | 0 | — | 500 | 0（刚到边界，0 偏移）|
| 1.0 * threshold + 1*MAD | 1*MAD | +1.0 | ~348 | -152（开始减仓，**从 0 开始而非 76%**） |
| 1.0 * threshold + 2*MAD | 2*MAD | +2.0 | ~307 | -193（接近饱和） |
| → +∞ | → +∞ | → +∞ | → 300 | -200（硬下限） |

**关键**：deviation>0（涨）→ excess>0 → u>0 → tanh>0 → -max_offset×正 →
target<base（减仓卖）。负号必要。

### 3.3 持仓追踪（内部状态 + 周期 reconcile）

```python
# 成功买:  self._holding += resp.shares
# 成功卖:  self._holding -= resp.shares
```

`setup()` 时从 `rest.get_holdings()` bootstrap 初值。之后**两层保障**：

- **内部增量**：每次下单后立刻更新 `_holding`，避免每次 event 都查 API
- **周期校准** (`reconcile_interval_sec`，默认 300s)：
  ```python
  if now - self._last_reconcile_ts >= self._reconcile_interval_sec:
      holdings = await ctx.rest.get_holdings()
      actual = next((h.amount for h in holdings if h.outcome_id == self._outcome_id), Decimal("0"))
      if abs(actual - self._holding) > self._reconcile_tolerance:
          self._logger.warning("volharvest_reconcile_drift_corrected",
                               actual=str(actual), internal=str(self._holding))
          self._holding = actual
      self._last_reconcile_ts = now
  ```

仍然建议 README 提示"策略运行期间不要手动 UI 交易该 outcome"，但即使违反，
reconcile 也能 5 分钟内把内部状态拉回真实——避免长跑漂移。

### 3.4 Trend guard（极简趋势过滤）

维护一个 `trend_window: deque[str] (maxlen=trend_guard_events)`，每个 SSE
trade event 时 push `trade["type"]`（"BUY" 或 "SELL"）。

```python
if len(self._trend_window) == self._trend_guard_events:
    all_same = len(set(self._trend_window)) == 1
    if all_same:
        side = self._trend_window[0]
        if side == "BUY" and delta < 0:
            # 散户连续追涨，我们想反向卖 → 不允许（怕被 trend 反向打穿）
            return skip("trend_guard_up_blocks_sell")
        if side == "SELL" and delta > 0:
            return skip("trend_guard_down_blocks_buy")
# 同向（散户在跌我们也想卖）/ 静态（trend_window 不满）/ 混合 → 放行
```

注意：**减仓方向同 trend 时不拦**——比如散户在追涨（trend up），我们想买
（同向 long 加仓）是允许的（虽然策略本身不会这么做，因为涨太多 deviation>0 →
target<base → delta<0 → 想卖，会被 guard 拦下）。

### 3.5 Bootstrap guard

base_shares=500 一次性建仓 ≈ 50 笔小单。Bootstrap 期间不能机械买：

```python
def _should_bootstrap(self, now, deviation, window_warm):
    if self._holding >= self._base_shares:
        return False
    if now - self._last_bootstrap_ts < self._bootstrap_interval_sec:
        return False
    if window_warm and self._bootstrap_skip_if_overpriced and deviation > 0:
        return False  # 价格偏高，等回落再补
    return True
```

bootstrap 路径**不**经过主信号流程（不算 trend guard、不算 deviation）；
就是单纯"分散把底仓补到位"。完成后退出 bootstrap mode，进主信号流程。

### 3.6 PnL 解释（重要）

`base_shares=500` 是**库存池（inventory）不是 alpha 判断**。策略本身在
"价格偏离 vs 持仓偏离底仓"之间做反向操作；base 大小只决定能"在多大空间内
震荡收割"。

**收益评估时必须拆分**：

```text
total_pnl = base_pnl + offset_pnl
  base_pnl   = (current_price - bootstrap_avg_price) * base_shares
             ← 这是 directional bet（押 YES 涨）的损益，跟波动率收割无关
  offset_pnl = Σ(每次 offset 调整的回归收益)
             ← 这才是策略真正赚的"波动率 alpha"
```

dry-run 和实盘日志的 `volharvest_trade` 事件里必须带 `bootstrap_mode` 字段，
让复盘能分清"这次买是建底仓"vs"这次买是 offset 调整"，否则没法算 offset_pnl。

Phase 2 可能改成 net inventory 模式（YES - NO 净持仓，base 为 0）消除
directional risk——但 Phase 1 接受 base 是 inventory 风险。

## 4. Config schema

```yaml
- name: volharvest_market_1_outcome_1
  type: volharvest
  enabled: true
  market_id: 1
  outcome_id: 1
  
  # 价格统计（logit 空间）
  window_size: 100              # 滑窗 N 笔 SSE trade event
  k_sigma: 2.0                  # 触发阈值 = k_sigma * (1.4826 * MAD) ≈ k_sigma * σ
  scale_mad: 1.0                # deadband 之外 tanh 的尺度（u = excess / (scale_mad * MAD)）
  min_mad_logit: 0.01           # MAD 太小时跳过，防除零和过敏感
  
  # 仓位
  base_shares: 500.0            # 目标底仓（=inventory，**非** alpha 判断）
  max_offset_shares: 200.0      # tanh 饱和上限 → 持仓范围 [300, 700]
  min_trade_shares: 5.0         # |delta| < 此值不下单
  max_trade_shares: 20.0        # |delta| > 此值 clip 到 ±max_trade_shares
  
  # Bootstrap
  bootstrap_max_step: 10.0      # 每次 bootstrap 最多补 N shares
  bootstrap_interval_sec: 30    # bootstrap 节流（避免每个 event 都补）
  bootstrap_skip_if_overpriced: true  # 价格偏高（deviation>0）时暂停 bootstrap
  
  # 周期校准
  reconcile_interval_sec: 300   # 5 分钟读一次真实持仓校准 _holding
  reconcile_tolerance: 1.0      # 差异 > 此值才覆盖 + log warning
  
  # 趋势过滤
  trend_guard_events: 5         # 最近 N 笔 trade 全同向 → 暂停逆势加仓
```

**字段必填性**：除 enabled/name/type 外全部必填；缺失抛 KeyError（trader.py 已有友好提示）。

## 5. 模块清单

| 文件 | 操作 | 内容 |
|---|---|---|
| `quant/thccb_quant/strategy/volharvest.py` | **新建** | VolatilityHarvest 类，`@register("volharvest")` |
| `quant/thccb_quant/trader.py` | **修改** | 顶部加 `import thccb_quant.strategy.volharvest` |
| `quant/config.example.yaml` | **修改** | 加 volharvest 示例条目（`enabled: false`） |
| `quant/tests/test_strategy_volharvest.py` | **新建** | 单元 + 集成 dispatch + replay 三类测试 |

**不需要的**：不动 Store / Rest / Broker / SseClient / 其他策略。本策略完全使用现有 API。

## 6. 错误处理

按 `writing-quant-strategy` skill 规范：

| 场景 | 处理 |
|---|---|
| `broker.buy/sell` 抛 `RiskRejected / BusinessError / TransientError` | log_decision skip + reason + 不更新 _holding + 下次 event 继续 |
| `broker.buy/sell` 抛 `FatalAuthError` | 不 catch，冒泡到 trader 触发全局停机 |
| SSE event 字段缺失（如 post_market_price / type / timestamp 不存在） | log warning + skip event（不能 fake） |
| logit clamp 后 p = 0.001/0.999 | 接受，正常计算（事后日志看到边界 clamp 频繁说明市场极端） |
| `rest.get_holdings()` 在 setup 失败 | 抛异常让 trader 启动失败（启动期错误不该 swallow） |
| `rest.get_holdings()` 在周期 reconcile 失败 | log warning + 跳过本次 reconcile（下次再试），策略继续按 internal _holding 跑 |
| bootstrap 期间下单失败 | log skip + 下次 event 继续尝试，直到 holding 达 base 或被风控全卡死 |
| trend guard 拦截 | log skip + 不计入失败统计（这是设计内行为不是异常） |

## 7. structlog 事件

按 SKILL 要求，关键决策都 emit。**字段尽可能丰富**让复盘不用对照 SQLite：

```python
# 信号触发或被 deadband/min_trade/clip 拦截 — 每次 event 都打
ctx.logger.info("volharvest_signal",
                price=str(current_price),
                logit_price=current_logit,
                median_logit=median_logit,
                mad=mad_logit,
                threshold=threshold,
                deviation=deviation,
                excess=excess,
                raw_target=raw_target,
                clipped_target=clipped_target,
                delta=delta,
                side="buy" | "sell" | "hold",
                reason="ok" | "deadband" | "min_trade" | "clipped"
                       | "trend_guard_up" | "trend_guard_down" | "bootstrap_overpriced",
                current_holding=str(self._holding),
                window_len=len(self._window),
                window_age_seconds=float(now - self._window[0].ts) if self._window else 0,
                bootstrap_mode=self._holding < self._base_shares)

# 成功下单 — broker 也会 emit order_success，但这里加策略上下文
ctx.logger.info("volharvest_trade",
                side="buy" | "sell",
                shares=str(resp.shares),
                cost=str(resp.cost),
                bootstrap_mode=True | False,
                holding_after=str(self._holding))

# Bootstrap 节流跳过 / overpriced 跳过
ctx.logger.info("volharvest_bootstrap_skip", reason=..., holding=str(self._holding))

# 预热/低波动跳过（频率低，每次 event 都打没关系）
ctx.logger.info("volharvest_window_not_warm",
                window_len=len(self._window), needed=self._window_size // 2)

# 周期 reconcile 校准（漂移时 warning，正常时 debug-level 跳过 emit）
ctx.logger.warning("volharvest_reconcile_drift_corrected",
                   actual=str(actual), internal=str(prev_holding),
                   diff=str(actual - prev_holding))

# Trend guard 触发
ctx.logger.info("volharvest_trend_guard_blocked",
                guard_side="BUY" | "SELL",
                blocked_action="sell" | "buy",
                last_n_sides=list(self._trend_window))
```

复盘示例：
```bash
# 所有"想下单但被拦"的情况
jq 'select(.event=="volharvest_signal" and .reason!="ok")' logs/system.jsonl

# offset_pnl 计算只看 non-bootstrap 的 trade
jq 'select(.event=="volharvest_trade" and .bootstrap_mode==false)' logs/system.jsonl

# trend guard 拦截频率（如果太高说明 trend_guard_events 设太松）
jq 'select(.event=="volharvest_trend_guard_blocked") | .guard_side' logs/system.jsonl | sort | uniq -c
```

## 8. 测试

按 SKILL 三类：

### 8.1 单元测试

**logit / 统计**：
- `test_logit_conversion_correct`: `to_logit(0.5)==0`, `to_logit(0.62)≈0.49`，clamp 边界
- `test_window_not_warm_skips`: 窗口少于 N//2 时不触发
- `test_low_mad_skips`: MAD < min_mad_logit 时跳过

**Deadband + tanh（关键，防 76% 跳变 regression）**：
- `test_within_deadband_target_is_base`: deviation = 0.5 * threshold → target == base，无下单
- `test_just_past_threshold_starts_from_zero`: deviation = threshold + 0.01*MAD → target 偏 base 极小（验证 tanh(小数) 行为）
- `test_no_tanh_jump_at_threshold`: deviation 从 0.99*threshold → 1.01*threshold，target 变化幅度 < max_offset * 0.05（验证连续性）
- `test_tanh_saturation_buy`: 大负偏离（excess >> mad）→ target ≈ base + max_offset → buy
- `test_tanh_saturation_sell`: 大正偏离 → target ≈ base - max_offset → sell

**仓位调节**：
- `test_min_trade_shares_filter`: |delta| < min_trade_shares 时跳过
- `test_max_trade_shares_clipped`: |delta| > max_trade_shares 时 clip 并 log
- `test_holding_update_on_success`: 下单成功后 `_holding` 正确递增/递减
- `test_holding_no_update_on_failure`: 下单失败 `_holding` 不变

**Trend guard**：
- `test_trend_guard_up_blocks_sell`: 连续 N 笔 BUY，策略想 sell → skip
- `test_trend_guard_down_blocks_buy`: 连续 N 笔 SELL，策略想 buy → skip
- `test_trend_guard_mixed_passes`: trend_window 混合 side → 不拦
- `test_trend_guard_not_full_passes`: trend_window 不满 N → 不拦

**Bootstrap guard**：
- `test_bootstrap_interval_throttle`: 两次 bootstrap 间隔 < interval_sec → 跳过
- `test_bootstrap_skip_overpriced`: window 已热 + deviation>0 → 跳过 bootstrap
- `test_bootstrap_step_capped`: base-holding > max_step 时只买 max_step

**Reconcile**：
- `test_reconcile_corrects_drift`: mock get_holdings 返 actual=450 而 internal=500 → 校正后 _holding=450 + warning log
- `test_reconcile_throttled`: 两次 reconcile 间隔 < interval_sec → 跳过 API 调用
- `test_reconcile_tolerance_no_log_if_close`: |actual - internal| < tolerance → 不警告不覆盖

### 8.2 集成（SseSubscriber dispatch）

- `test_subscriber_routes_to_volharvest`: 真 SseSubscriber + mock SseClient 喂 trade event，验证 on_sse_event 被调
- `test_bootstrap_buys_on_first_event_if_holding_below_base`: setup mock `get_holdings()` 返 100，base=500，第一笔 event 触发 buy(10) (bootstrap_max_step)

### 8.3 Setup / Holdings 初始化

- `test_setup_reads_actual_holdings_as_base`: mock `get_holdings()` 返 500，setup 后 `self._holding == 500`，无 bootstrap
- `test_setup_below_base_enters_bootstrap`: mock 返 100，setup 后进 bootstrap mode
- `test_setup_above_base_no_action`: mock 返 800（高于 base 500），不主动卖；超额仓位由后续 tanh 信号自然消化

## 9. dry-run 准入

按 SKILL pre-prod 清单。**60s 不够**——thccb 流动性低时 window 都没预热完。
正确准入：

### 第 1 阶段：短跑（验证不崩）
```bash
cd quant && source .venv/bin/activate
pytest -x                           # 全过才能跑
# config.yaml enable volharvest 指向真实 outcome
python -m thccb_quant --dry-run     # 跑 60s
touch state/KILL                    # 优雅停
```
看 `logs/system.jsonl` 应见：
- `startup` / `sse_partial_trades_preloaded` / `stopped_clean`
- 至少几条 `volharvest_window_not_warm`
- 启动 bootstrap 阶段几条 `volharvest_bootstrap_skip`（节流）+ 1-2 笔 `order_dryrun`
- **无任何 ERROR**（除 `refresh_token_expiring_soon` 等已知）

### 第 2 阶段：长跑（验证主信号）
继续跑 ≥6 小时（或挂 tmux 跑 24h），目标：
- 看到 ≥100 条 `volharvest_signal` event（说明 window 预热完且主流程在跑）
- `jq 'select(.event=="volharvest_signal" and .reason=="ok") | .delta'` 至少 10-20 条非 0 delta
- 至少触发过一次 trend_guard（说明保险丝在工作）
- 至少触发过一次 reconcile（5min × 数小时 = 至少 12 次，应无 drift warning）
- `decisions` 表 + `orders` 表行数合理

### 第 3 阶段：上实盘前微调
- 把 `risk.max_slippage_bps` 从 300 调到至少 **800**（高波动市场默认会被大量拒，
  参见 memory `project-market-volatility`）
- `base_shares` 第一次实盘建议先 200（小于 spec 的 500），观察 1-2 天 PnL
  拆分（base_pnl vs offset_pnl）符合预期再放大
- `risk.daily_loss_cap_cny` 设为 base_shares × max_avg_price × 0.5 的量级
  （volharvest 极端情况可能日内浮亏 base 的一半）

### Phase 2 待办（不阻塞 Phase 1 上线）
- 写一个 `quant/scripts/replay_volharvest.py` 用 trades 表历史数据 replay
  策略，验证 500-1000 个 event 的决策符合预期

## 10. 风险清单

| 风险 | 影响 | 缓解 |
|---|---|---|
| trend 真转，价格不再回归 | 持仓推到 max_offset 边界卡住 | (a) max_offset_shares 硬上限 (b) trend_guard 减少逆势加仓 (c) 接受这是 mean reversion 固有风险 |
| LMSR 滑点吃利润 | 预期回归 5% 实际可能赚 2-3% | 接受；real-money smoke 看 cost vs quote 偏差，必要时收紧 min_trade_shares 减少高滑点小单 |
| base_shares 是 directional bet | base_pnl 跟 strategy 真正 alpha 混在一起 | §3.6 PnL 拆分公式 + `volharvest_trade` event 带 `bootstrap_mode` 字段；评估时分开算 |
| 启动 bootstrap 拉价 / 在错误时机机械买 | base=500 启动 ≈ 50 笔小单；价格偏高时还机械买 = 追高 | (a) `bootstrap_interval_sec=30` 节流 (b) `bootstrap_skip_if_overpriced` (c) `bootstrap_max_step=10` 单次小 |
| 手动 UI 交易撞策略 | _holding 与真实不同步 | (a) README 警告 (b) **`reconcile_interval_sec=300` 周期校准**，5 分钟内自动拉回 |
| 滑窗 N=100 低流动性下预热慢 | 启动前 ~50 笔交易内不动作 | 接受；`volharvest_window_not_warm` 日志可观测进度；准入要求看到 ≥100 个 signal event |
| `max_slippage_bps: 300` 默认在 ±10% 波动市场大量拒单 | 触发但下不出单 | dry-run 时若大量 `RiskRejected`，调宽到 800-1500（spec §9 第 3 阶段提及） |
| 信息事件（真新闻）伪 noise → 反向被套 | 持仓推到 max_offset 卡住 | trend_guard 是 Phase 1 的最小过滤；max_offset 是硬上限。Phase 2 可加成交量/连续性过滤 |

## 11. 范围外（YAGNI）

**Phase 1 不做**：
- 信息事件高级过滤（仅做 `trend_guard_events` 最小版；成交量/速度/大单冲击检测留 Phase 2）
- Net inventory 模式（YES - NO 净持仓 + base=0；Phase 1 保留 base_shares 作为 inventory）
- 历史 trades replay 工具（dry-run 准入靠"长跑看 ≥100 signal events"代替；Phase 2 写 `scripts/replay_volharvest.py`）
- 多 outcome 联动 / 跨市场套利
- 参数自动调优 / 网格搜索
- 显式止损单（max_offset 是仓位上限，没有"亏到 X 强制平"）
- 自适应 window_size / k_sigma（按市场状态自动调）
- 与其他策略协调下单（DCA + VolatilityHarvest 同 outcome 资金冲突由 risk.daily_turnover_cap 兜底）

## 12. 估算

- 代码：策略文件 ~350-400 行（含 trend guard / reconcile / bootstrap guard）
- 测试：~450-550 行（25+ 测试覆盖 deadband / saturation / trend / reconcile / bootstrap）
- 实施：1 个 plan task，subagent 一次干完
- 时间：subagent ~40-50 分钟 + review 15-20 分钟

## 13. 修订记录

**v1 → v2（本版）：基于用户 spec review 的 5 个核心改动**
1. **Deadband + tanh 重写**：旧版刚过阈值跳 76% 饱和；新版 deadband 内 target=base，
   过线后从 0 开始平滑（§3.2）
2. **base_shares 性质澄清**：明确是 inventory 不是 alpha 判断 + PnL 拆分公式（§3.6）
3. **Bootstrap 条件化**：加 `bootstrap_interval_sec` 节流 + `bootstrap_skip_if_overpriced`（§3.5）
4. **周期 reconcile**：加 `reconcile_interval_sec` 自动校准 `_holding` 防漂移（§3.3）
5. **Trend guard**：加 `trend_guard_events` 最小趋势过滤防被真趋势打穿（§3.4）

**配套改动**：
- 参数 `k_mad` → `k_sigma`，公式加 `1.4826 * MAD` 让 k 直观等价于 σ 倍数
- 加 `max_trade_shares` 单笔 shares 上限
- logging 字段大幅扩充（§7）
- dry-run 准入从"60s"改成"短跑+长跑+实盘前微调"三阶段（§9）
- 测试新增 deadband 连续性、trend guard、reconcile、bootstrap guard 一组（§8.1）
- §11 YAGNI 加 net inventory 模式 + replay 工具留 Phase 2
