# Partial Liquidation 设计 spec

> **Status**: Approved
> **Owner**: Renko6626
> **Created**: 2026-05-20
> **Branch**: `feat/partial-liquidation`
> **Related**:
> - `docs/superpowers/specs/2026-05-18-forced-liquidation-design.md` (原 all-in 强平)
> - `docs/holdings-value-semantics.md` (MTM/LCV 双口径)
> - `docs/archive/liquidation-perf-options-2026-05-18.md` (perf 优化档，已归档)

## Goal

把当前"margin < 0.2 → 全平所有持仓"改成 partial 平仓：每 sweep tick 只平 10%，
靠多 tick 渐进达到 target margin (0.3)。仅当用户净值跌穿 0.05（接近资不抵债）
时才走 emergency 全平路径。

**生产推荐配置**：
- `liquidation_sweep_interval_sec = 10` (10s 一波，admin 上线后改)
- `liquidation_partial_pct = 0.10`
- `liquidation_target_margin = 0.30`
- `liquidation_emergency_threshold = 0.05`

按此配置，user margin 跌穿 hard_threshold (0.20) 后约 **2-3 波 = 20-30s 收敛**
到 target (0.30)。

## 非目标 (Non-Goals)

- 不解决"用户主动 sell 时的 cost_basis 处理"——这是 partial liquidation 才用的
  路径（现有 market.py sell 路径已用同款"按比例减少 cost_basis"）
- 不引入"用户主动追加保证金"功能（用户已可主动 sell / 借更少）
- 不做"按需求精确算"算法（参考 brainstorming 讨论里的算法 B）—— 数学复杂，10%
  按比例算法 A 已够用
- 不重写 LiquidationEvent admin UI（SQLAdmin auto-CRUD 足够）

## 动机：当前 all-in 的 3 个真实风险

1. **过度反应**：margin 0.19 → 全平。但用户净值 = 19% × debt > 0，平掉一小部分
   把 margin 拉回 0.3+ 就够，"过度治疗"
2. **级联爆仓 (LMSR 特有)**：一次性砸大卖单 → 同 market 其他高杠杆用户 margin
   跌穿 → 触发第二波强平 → flash crash。LMSR `b=100` 单笔可 ±10%
3. **自滑点放大**：卖 200 股 LCV 滑点可能吃掉 25%；平 20 股只吃 2-3%

业界 (Binance/OKX Smart Liquidation / dYdX) 都用 partial。

## Architecture overview

```
sweep tick (默认 600s / 推荐生产改 10s)
  ↓
拿 LCV margin < hard_threshold (0.2) 的用户（perf 已优化批量预筛）
  ↓
对每个 user 调 liquidate_user(...)
  ↓
liquidate_user 内部:
  pre_margin = (cash + LCV - debt) / debt
  
  if pre_margin < emergency_threshold (0.05):
    mode = "emergency"  → 全平所有 position
  else:
    mode = "partial"    → 每 position 卖 partial_pct (10%)
  
  ↓
按 market group 锁 outcomes + 算 LMSR proceeds
  ↓
应用变更：
  - emergency: pos 删除，total_shares -= 全 amount
  - partial: pos.amount -= sell_amount, pos.cost_basis -= cost_reduced
             total_shares -= sell_amount
  ↓
还债 + 写 LiquidationEvent (mode=...)
```

**关键设计**：
- partial 模式下**单 tick 只平一小部分**，不在 tick 内循环
- 收敛靠多 tick：下个 sweep tick 重新评估 margin，如果已达 target → 不触发
- emergency 兜底：避免 user "永远 margin < 0.05 但每 tick 只平 10%" 死循环

## 数据模型

### `LiquidationEvent` 加一个字段

```python
class LiquidationEvent(SQLModel, table=True):
    # ... 现有字段不变 ...
    mode: str = Field(default="emergency", max_length=20)
    # "partial" / "emergency"
    # 历史 row (此 PR 之前的) 默认 "emergency" 兼容
```

**Alembic migration**：`ADD COLUMN mode VARCHAR(20) NOT NULL DEFAULT 'emergency'`。
历史 row 自动填 "emergency"（符合事实——之前实现就是全平）。

### Site_config 新 keys（3 个）

| Key | Type | 默认 | 含义 |
|---|---|---|---|
| `liquidation_partial_pct` | decimal | `0.10` | 每波平的比例（相对持仓 amount） |
| `liquidation_target_margin` | decimal | `0.30` | 收敛目标 margin（用户重新进入正常区间）|
| `liquidation_emergency_threshold` | decimal | `0.05` | margin < 此值 → 跳 partial，直接全平 |

注：`liquidation_sweep_interval_sec` **不改默认值**（保持 600s 兼容现有部署），
admin 上线后手动改成 10s（详见"部署"章节）。

## 关键流程

### `liquidate_user()` 改造

伪代码（实际改 `backend/app/services/liquidation_service.py`）：

```python
async def liquidate_user(
    session, user, *, daily_rate, trigger_source,
    target_margin: Decimal, partial_pct: Decimal, emergency_threshold: Decimal,
) -> LiquidationEvent:
    if user.debt <= ZERO:
        raise ValueError("user.debt > 0 required")
    
    # pre-snapshot (现有逻辑)
    pre_cash, pre_debt = user.cash, user.debt
    pre_hv = (LCV 计算 inline)
    pre_nw = pre_cash - pre_debt + pre_hv
    pre_margin = pre_nw / pre_debt
    
    # 决策 mode
    mode = "emergency" if pre_margin < emergency_threshold else "partial"
    
    # 拿 positions + lock outcomes (现有逻辑)
    positions = ...
    outcomes_by_market = ...
    
    total_proceeds = ZERO
    sold_count = 0
    
    for market_id in sorted(...):
        for pos in pos_group:
            # 算 sell_amount
            if mode == "emergency":
                sell_amount = pos.amount
            else:
                # partial: 向上取整到整数股 (玩家体感"卖 X 股不是 0.5 股")。
                # 零碎小数持仓 ceil 后 >= 1 → 触发下面 clamp 全卖，自然清掉。
                sell_amount = (pos.amount * partial_pct).quantize(
                    Decimal("1"), rounding=ROUND_CEILING
                )
            
            if sell_amount <= ZERO:
                continue  # 防数值边界 (实际仅 pos.amount=0 时发生)
            
            # LMSR proceeds (现有算法)
            old_cost = calculate_lmsr_cost(shares_list, b)
            new_q = shares_list[:]
            new_q[idx] -= float(sell_amount)
            new_cost = calculate_lmsr_cost(new_q, b)
            proceeds = quantize_cost(old_cost - new_cost) * (1 - SELL_FEE_RATE)
            
            if proceeds < ZERO:
                continue  # skip 负 proceeds 持仓 (现有逻辑)
            
            # 应用变更
            user.cash += proceeds
            all_outcomes[idx].total_shares -= sell_amount
            
            if sell_amount >= pos.amount:
                # 全卖 (emergency 或 partial 时 amount × 0.1 ≥ pos.amount 边界)
                await session.delete(pos)
            else:
                # partial 卖
                # cost_basis 按"真实卖出比例" sell_amount/pos.amount 减少，
                # 严格保持 avg_price = cost_basis/amount 不变（平均成本法）。
                # 注：用 sell_amount/pos.amount 而非 partial_pct，避免 sell_amount
                # 被 quantize 后引入的 sub-LSB 漂移（review I-3）。
                cost_reduced = (pos.cost_basis * sell_amount / pos.amount).quantize(Decimal("0.000001"))
                pos.amount -= sell_amount
                pos.cost_basis -= cost_reduced
            
            # 写 Transaction (现有逻辑，price/cost/gross/fee 都按 sell_amount 算)
            
            total_proceeds += proceeds
            sold_count += 1
    
    # 还债 (现有逻辑用 decrease_debt_locked)
    repaid = ZERO
    if user.cash > ZERO and user.debt > ZERO:
        repay_amount = min(user.cash, user.debt).quantize(_QUANT)
        repaid = await loan_service.decrease_debt_locked(...)
    
    # 写 LiquidationEvent
    if sold_count == 0 and repaid == ZERO:
        # 完全 noop (HALT 持仓全跳过 / 所有 proceeds < 0)
        return _noop_event(...)
    
    user.last_liquidated_at = datetime.now(timezone.utc)
    ev = LiquidationEvent(
        user_id=user.id,
        triggered_at=now,
        pre_cash=pre_cash, pre_debt=pre_debt, pre_holdings_value=pre_hv,
        pre_net_worth=pre_nw, pre_margin_ratio=pre_margin,
        sold_positions_count=sold_count,
        total_proceeds=total_proceeds,
        repaid_amount=repaid,
        remaining_debt=user.debt,
        post_cash=user.cash,
        trigger_source=trigger_source,
        mode=mode,  # 新字段
    )
    session.add(ev)
    return ev
```

### sweep tick 传参

`liquidation_sweep.py` 的 `_liquidate_one_user` 调用 `liquidate_user` 时传新参数：

```python
ev = await liquidation_service.liquidate_user(
    session, user, daily_rate=rate,
    trigger_source=trigger_source,
    target_margin=target_margin,    # 新
    partial_pct=partial_pct,         # 新
    emergency_threshold=emergency_threshold,  # 新
)
```

`run_liquidation_sweep_once()` 主循环开头一次读 site_config 拿这 3 个值。

注：`target_margin` 当前**只用于 metric/logging**（写到日志方便 admin debug），
**不在 liquidate_user 内部决策**——partial 模式下永远只平 partial_pct，
不会"动态判断要不要继续平"（避免单 tick 内循环复杂度）。多 tick 自动收敛靠
"下个 tick 看 margin 已升到 target 就不再触发"。

### Cost basis 处理

partial 卖时（设 `r = sell_amount / pos.amount`，即真实卖出比例，理论上 ≈ partial_pct
但 sell_amount 经 quantize 后会有 sub-LSB 偏差）：
- `new_amount = old_amount - sell_amount = old_amount × (1 - r)`
- `new_cost_basis = old_cost_basis - old_cost_basis × r = old_cost_basis × (1 - r)`
- 因此 `avg_price = cost_basis / amount` **严格不变**（平均成本法）

注：实现里 `cost_reduced = (pos.cost_basis * sell_amount / pos.amount).quantize(...)`
而**不是** `pos.cost_basis * partial_pct`，原因就是用 partial_pct 在 sell_amount
quantize 后会让 avg_price 漂移（review I-3）。

这跟用户主动 `/market/sell` 部分卖出的语义一致（看 market.py:sell_outcome），
所以"被 partial 强平"vs"主动卖出"在 cost_basis / avg_price 视角下等价。

### Mode 决策

```
margin < emergency_threshold (0.05)  → emergency 全平
margin ∈ [emergency, hard_threshold) → partial 10%
margin ≥ hard_threshold (0.20)       → 不触发 (现有 sweep 逻辑过滤)
```

`emergency_threshold < hard_threshold`（保证逻辑互斥）。

## 错误处理 + edge cases

### partial_pct 边界

| 配置值 | 行为 |
|---|---|
| `partial_pct = 0` | 无意义（不平任何东西）。代码不主动校验；admin 自己别这么设 |
| `partial_pct = 1.0` | 等于 emergency 全平。代码自然正确（sell_amount = pos.amount） |
| `partial_pct = 0.5` | 每次平 50%。合理配置 |

### sell_amount 极小

partial 模式下 `pos.amount × 0.10` 在小持仓时可能产生 `0.000001` 级数字。
quantize 到 6 位 → 等于 `0.000000` → `if sell_amount <= ZERO: continue` 跳过。
也算"软退化"，不报错。

### LMSR proceeds < 0

partial 同 emergency 一样 `continue` 跳过该 position（不动）。日志记录。

### 多 tick 收敛失败

理论上 LMSR 价格变化可能让 partial 多波后仍达不到 target。极端例子：
- user 持仓 100 股 outcome A，每波卖 10 股
- 同 market 其他 user 也在巨量买入 → outcome A 价格涨
- 但 user 同时还在 buy（如果 site 允许）→ debt 增加
- partial 永远追不上

mitigation：
- emergency_threshold 是兜底——只要 margin 继续跌穿 0.05 就 all-in
- _recently_attempted cache (现有逻辑) 30 min cooldown 防 noop spam

### LiquidationEvent.mode 历史 row

migration 加列默认 `"emergency"`（事实：此 PR 之前全是全平）。SQLAdmin 列表能看
到字段，但旧 row 没有 partial 用例。

### test fixture 中 mode 字段

新增测试创建 LiquidationEvent 时显式传 `mode=`（避免漏 default 引起回归）。

## 测试矩阵

### `backend/tests/test_liquidation_partial.py` (新)

- **partial 单波**：cash=100, debt=900, LCV=800, margin = 0/900 → 现实没意义，
  调整为 cash=0, debt=1000, LCV=900, margin = -100/1000 = -0.1 → 落入 hard
  但 not emergency (margin > 0.05? 实际 -0.1 < 0.05 是 emergency)
  
  正确场景：cash=100, debt=1000, LCV=1000 → margin = 100/1000 = 0.1
  → mode=partial, 平 10% LCV ≈ 100, debt 还 100+ ≈ 还到 900
  → margin 上升到 ~0.11 (单波)
  
  断言：写 LiquidationEvent.mode = "partial", sold_count > 0,
  剩余 position.amount > 0, cost_basis 按比例减少

- **emergency**：margin < 0.05 → mode = "emergency"，全平（同现有逻辑）
  - 断言 LiquidationEvent.mode = "emergency", session.delete(pos) 被调

- **partial 多 tick 收敛**：seed user margin=0.1，跑 5 个 sweep tick
  （清 _recently_attempted 之间）→ 最终 margin > target_margin

- **cost_basis 按比例**：partial 后 `new_cost_basis / new_amount == old_cost_basis / old_amount`
  即 avg_price 不变

- **partial_pct = 1.0 等价 emergency**：行为应等同全平

- **sell_amount = 0 跳过**：seed 极小持仓（如 amount=0.000005），partial × 0.1
  量化到 6 位为 0 → skip + 不抛错

- **partial 在 HALT 市场跳过**：跟现有逻辑一致

- **LiquidationEvent.mode default**：历史 row 没 mode 字段 → migration 加列默认
  "emergency"（手工写 SQL 模拟 / 或测 ORM model default）

### 现有测试更新

- `test_liquidation_service.py` 的 happy_path test 现在 margin 设置低于
  emergency_threshold → 走 emergency 路径（保持现有行为）；或设到 partial 区间
  改用新 assertion
- `test_liquidation_sweep.py` 集成测：调用 `run_liquidation_sweep_once()` 时新
  参数从 site_config 读，断言不变

## 部署步骤

### 1. PR merge → CI 自动跑

- Alembic migration 加 LiquidationEvent.mode 列（DEFAULT 'emergency'）
- `auto_migrate()` seed 3 个新 site_config keys（partial_pct 0.10, target 0.30,
  emergency 0.05）
- Backend / frontend 同步上线

### 2. 部署后默认行为

- `liquidation_enabled` 仍 false (现有默认) → sweep 不实际跑
- 即使开 enabled，sweep_interval 还是 600s → 用户 1 次 partial 10 min 才下一波

**所以 PR merge 上线本身行为 0 变化**（兼容现有部署）。

### 3. 启用 partial（admin 操作）

活动前 admin 在 SQLAdmin → site_config 改：

```
liquidation_enabled        = true     (启用强平)
liquidation_sweep_interval_sec = 10   (10s 高频, 推荐生产值)
```

可选调（默认值已合理）：
- `liquidation_partial_pct` = 0.10
- `liquidation_target_margin` = 0.30
- `liquidation_emergency_threshold` = 0.05

### 4. Admin 调整指南

如果选不同 sweep_interval，相应调 partial_pct 保持"约 2 波收敛"体感：

| interval | 推荐 partial_pct | 单 user 收敛时间 |
|---|---|---|
| 10s | 0.10 | ~20-30s |
| 60s | 0.20 | ~2 min |
| 600s | 0.30 | ~20 min |

interval 越长，partial_pct 应越大（避免收敛慢得离谱）。

### 5. 回滚

- 紧急情况：admin 改 `liquidation_partial_pct = 1.0` → 立刻退化到全平（旧行为）
- 或者 `liquidation_enabled = false` 整个停掉

## 风险评估

| 风险 | 概率 | 影响 | 应对 |
|---|---|---|---|
| Migration `ADD COLUMN mode` 在大 LiquidationEvent 表上慢 | 低（hobby 表不大）| 部署慢几秒 | accept |
| partial_pct 极小（如 0.01）让收敛极慢 | 低（admin 不太可能误配）| 用户长期被绑定 | spec 推荐值 + 调整指南 |
| LMSR 价格剧烈变动让多 tick 收敛失败 | 低 | emergency 兜底自动接管 | 已 design |
| 10s sweep 高频 → DB / scheduler 压力 | 已知（perf 测过 100 用户 ~70ms）| 0.7% CPU 可忽略 | accept |
| LiquidationEvent 表爆膨胀（10s × N user × partial 多波）| 中（24h × 6 × 10 user × 3 波 = 4320 行/天）| 长期累积 | 加 cron 清理 30+ 天 `review_status != 'pending'`（不在 spec 范围）|

## 后续迭代（不在本 spec 范围）

1. **LiquidationEvent 清理 cron**：删 30+ 天已 review row
2. **partial 算法 B**：按需求精确算（数学复杂，仅在用户量大需要时考虑）
3. **追加保证金 API**：用户主动 `/loan/repay` 已经支持，不需要新接口
4. **partial 通知**：用户被 partial 平后给一个 SSE 推送 / email？hobby 站暂不

## 参考

- 类似 scheduler：`backend/app/services/liquidation_sweep.py` (10s 高频跑前提)
- 锁顺序：`docs/holdings-value-semantics.md` § 流动性危机保护守卫
- 业界对照：调研简报里 Binance / dYdX partial liquidation
