# MeanRev 策略设计稿

**日期**：2026-05-18
**位置**：`quant/thccb_quant/strategy/meanrev.py`
**定位**：VolHarvest 的"激进 + 极简"对照版

## 1. 动机

VolHarvest 用了 logit + MAD + deadband + tanh + bootstrap + reconcile + trend
guard 一堆保守化设计。在 TouhouCCB 的 LMSR 市场（单笔可 ±10%）里，"主动让价格
偏离均值" 的人会被均值回归套利者收割（详见 `feat/quant-trader` 上一对话的诊断）。

`meanrev` 利用 bot 反应速度比人快这一点，**做那个均值回归套利者**：实时盯 SSE
事件，价格一偏离 EMA 就反向下单，不要任何 guard、不要 bootstrap 阶段、不要 MAD。

## 2. 适用前提（绑死，启动校验）

- **二元市场**：恰好 2 个 outcome（启动 setup 校验，否则 ValueError）
- **市场处于 trading**（broker 自己会兜底，策略不重复检查）
- **策略跑期间不在 thccb UI 上手动交易该 market**（避免持仓漂移；不做周期 reconcile）

## 3. 信号公式

```
price_A  = trade["market_prices_post"][index_of_A_in_sorted_outcomes]   # = mp[0]
logit_A  = log(p / (1 - p)), clamp p ∈ [0.001, 0.999]

# EMA 更新（每个 SSE trade event 都更，含 warmup 期）
ema_logit_A ←  α · logit_A + (1 - α) · ema_logit_A

# 预热闸门
if event_count < warmup_events: skip

# 偏离 + 阈值
deviation = logit_A - ema_logit_A
if abs(deviation) < threshold_logit: skip   # in deadband
```

**默认配置参考**（在 default config 里）：

| 参数 | 默认 | 含义 |
|---|---|---|
| `ema_alpha` | 0.10 | 半衰期 ≈ 7 笔 trade |
| `threshold_logit` | 0.15 | ≈ 价格偏 ±3.7%（p≈0.5 时）|
| `warmup_events` | 20 | 冷启动跳过前 20 笔 |

## 4. 决策树（核心 10 行）

```python
# 偏离越大下单越大：multiplier 在 threshold 处 = 1，饱和于 size_scale_cap
size_multiplier = min(|deviation| / threshold_logit, size_scale_cap)
target_amount = cash * trade_pct_of_cash * size_multiplier   # 想花/想回多少钱

if deviation > 0:                              # A 偏高 → "做空 A"
    sell_id, sell_price = id_A, price_A
    buy_id,  buy_price  = id_B, 1 - price_A    # binary: p_B = 1 - p_A
else:                                          # A 偏低 → "做多 A"
    sell_id, sell_price = id_B, 1 - price_A
    buy_id,  buy_price  = id_A, price_A

# 1) 优先卖对面（手上有货才卖，永不"做空式 sell"）
sell_shares = floor(target_amount / sell_price)
sell_shares = min(sell_shares, holding[sell_id])
if sell_shares >= min_trade:
    SELL sell_id sell_shares
    return

# 2) 否则买被低估的（受 max_holding_per_side cap）
buy_shares = floor(target_amount / buy_price)
buy_shares = min(buy_shares, max_holding_per_side - holding[buy_id])
if buy_shares >= min_trade:
    BUY buy_id buy_shares
    return

skip "size_below_min"
```

## 5. 内部状态（仅 5 个字段）

| 字段 | 类型 | 来源 / 更新 |
|---|---|---|
| `_ema_logit` | `Optional[float]` | 第 1 个 SSE event 直接 = current_logit；之后 α 平滑 |
| `_event_count` | `int` | 每个 SSE event +1 |
| `_holding` | `dict[int, Decimal]` | setup 从 `rest.get_holdings()` bootstrap；broker 成功后增减 |
| `_cash` | `Decimal` | setup 从 `rest.get_user_summary()` bootstrap；交易后 `_cash -= resp.cost`（BUY `cost>0` 减，SELL `cost<0` 加）。**不**用 `resp.new_cash` 因为 `DryRunBroker` 硬返回 0；`resp.cost` 在 live + dryrun 都是真值。 |
| `_outcome_ids` | `tuple[int, int]` | setup 时 `sorted(market.outcomes, key=lambda o: o.id)` 取前两个 |

**不持久化** `_ema_logit` / `_event_count` —— 重启后从零 warmup。理由：实现简单；
SSE 重连 catchup 会把丢失期间的 trades 推回来（虽然不进 EMA 但不漏单）；warmup
20 笔在活跃市场几分钟内完成。

## 6. Config schema

```yaml
- name: meanrev_market_1
  type: meanrev
  enabled: false
  market_id: 1
  ema_alpha: 0.10
  threshold_logit: 0.08
  warmup_events: 20
  trade_pct_of_cash: 0.05      # 在 |deviation|=threshold 时基础下单 = cash × 5%
  size_scale_cap: 3.0          # size = base × min(|deviation|/threshold, cap)；cap=1 关闭缩放
  min_trade: 10                # 取整后 < 此值直接 skip（过滤残余小数 + 边际触发）
  max_holding_per_side: 200    # 单边持仓上限（防 cash 一夜烧光）
```

**`__init__` 用 `int(config["..."])`/`Decimal(str(config["..."]))` 直读**，缺
字段就 KeyError 让 trader 友好报错（不要 `.get(..., default)`）。

**校验项**（init 抛 ValueError）：
- `0 < ema_alpha <= 1`
- `threshold_logit > 0`
- `0 < trade_pct_of_cash <= 1`

## 7. 风控 / 失败模式

| 情况 | 行为 |
|---|---|
| `cash` 不够 (broker `RiskRejected` "insufficient_cash") | catch + log_decision skip + return；不重试 |
| 单边持仓打到 `max_holding_per_side` | sizing 时 cap 到剩余，可能直接 skip |
| broker `BusinessError`（市场关、滑点拒）| catch + log_decision skip + return |
| broker `TransientError`（网络/5xx，已重试 3 次）| catch + log_decision skip + return |
| `FatalAuthError` | **不 catch**，让 trader 全局停机 |
| SSE event 字段坏 | `meanrev_bad_event` warning + return |
| `market_prices_post` 长度 < 2 | warning + return（不应该发生于二元市场，防御性）|

## 8. 测试矩阵

按 `writing-quant-strategy` skill 三类：

**单元**：
- `to_logit(0.5) ≈ 0`
- config 校验抛 ValueError
- setup 拒非二元市场
- setup 正确 bootstrap holdings + cash
- warmup 期内不下单
- deadband 内不下单
- 偏高 + 持有 A → 卖 A
- 偏高 + 不持 A → 买 B
- 偏低 + 持有 B → 卖 B
- 偏低 + 不持 B → 买 A
- sizing：cash × pct / price 正确取整
- sizing：`max_holding_per_side` cap 生效
- sizing：size < min_trade 时 skip
- `RiskRejected` 时记 skip decision，不抛

**集成**：用真 `SseSubscriber` + mock `SseClient` 验证事件路由到策略

**Replay 类不需要**：策略不持久化 `_ema_logit`；持仓走 `get_holdings` bootstrap。

## 9. 与 VolHarvest 的对照

| 维度 | VolHarvest | meanrev |
|---|---|---|
| 中心估计 | 滑窗 logit 中位数 | logit EMA |
| 离散度估计 | MAD + k_sigma | 无 |
| 阈值 | 自适应 (`k_sigma · 1.4826 · MAD`) | 固定 `threshold_logit` |
| 信号→size | tanh 平滑映射到 target position | 固定比例 `cash × pct` |
| 仓位 model | 单 outcome，带 base 底仓 | 跨 outcome，无 base |
| Bootstrap 阶段 | 有（节流补到 base）| 无 |
| Reconcile | 每 5min 校准 | 无 |
| Trend guard | 连续 N 笔同向拦逆势 | 无 |
| 反向"做空"实现 | 卖 self outcome（必须有货）| 卖对面优先，无货就买另一侧 |

## 10. 完成准入清单

- [ ] `pytest -x quant/tests/test_strategy_meanrev.py` 全过
- [ ] 单元 + 集成测试都有
- [ ] `python -c "from thccb_quant.trader import main_async"` 不报错
- [ ] `config.example.yaml` 加示例（含 `market_id`）
- [ ] `trader.py` 顶部加 `import thccb_quant.strategy.meanrev  # noqa: F401`
- [ ] 改 config enable 新策略 → dry-run 30s+ → 看 `decisions` 表确认决策符合预期
