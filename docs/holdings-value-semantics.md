# 持仓估值的两套口径

> TouhouCCB 后端对"持仓估值"有两种语义。这是 LMSR 流动性参数 `b≈100` 量级下
> 单笔可滑点 ±10% 的特殊性所要求的——一个数字无法同时满足"直观显示"和"保守
> 安全"两个目标。本文档给玩家+开发者解释这两个口径。

## 两个口径

### MTM（Mark-to-Market，账面瞬时估值）

```
MTM = Σ (amount × LMSR 边际价)
    = Σ (amount × exp(q_i / b) / Σ exp(q_j / b))
```

按当前 LMSR 边际价 × 持仓数量。**不含滑点、不扣手续费**。等价于"假设按
当前瞬时价交易小量股的估值"。

**用于**：
- `/user/summary` 主显示 `holdings_value` / `net_worth`
- 排行榜 `/market/leaderboard` 排序
- `unrealized_pnl = holdings_value (MTM) - cost_basis` 未实现盈亏（主显示）
- `/user/holdings` 每行 `unrealized_pnl = amount × current_price - cost_basis`（主显示）
- 称号 `rank_title()` 计算

### LCV（Liquidation Value，立即清算价值）

```
LCV = (C(q) - C(q_after_full_sell)) × (1 - sell_fee_rate)
其中 C(q) = b·log(Σexp(q_i/b))
```

按 LMSR cost diff 算「全部卖出能拿到的现金」**含全卖滑点 + 扣 sell_fee**。
通常 < MTM，差距 ≈ 滑点 + sell_fee。

**用于**：
- `/user/summary` 副字段 `holdings_value_liquidation` / `net_worth_liquidation` / `unrealized_pnl_liquidation`
- `/user/holdings` 副字段 `unrealized_pnl_liquidation = market_value - cost_basis`
  （与"卖出均价 = market_value / amount"自洽）
- `margin_ratio = net_worth_liquidation / debt` 保证金率判定
- 借款额度 `max_borrow` 上限（避免过度杠杆）
- 强平 sweep 触发判定 + 强平动作本身的 proceeds
- `LiquidationEvent` 历史快照 `pre_*` 字段
- `/admin/wealth` 财富统计（内部分析口径）

## 为什么要分裂？

如果只用 MTM：用户看到 NW=500，借款 800，账面 margin=0.625 → 显示 healthy。
但用户真要逃命卖出时只能拿到 350，实际 margin=0.438 → 突然被强平。**用户被
"虚高估值"骗了**。

如果只用 LCV：大持仓用户看到 NW 总是显示偏低（比账面少 20-30%），**心理上
被惩罚**，体感差。排行榜按 LCV 排序时小仓位玩家容易"虚高"（无滑点折扣）。

**所以分裂分工**：
- 直观可见的"我有多少钱" → MTM
- 关乎钱袋的"能借多少 / 会不会爆仓" → LCV

## 后果对照表

| 场景 | 口径 | 谁受益 |
|---|---|---|
| Portfolio 显示净资产 | MTM | 玩家心理舒服 |
| 排行榜 | MTM | 大仓位玩家排名不被低估 |
| 借款额度 max_borrow | LCV | 防止过度杠杆 |
| 强平触发线 | LCV | 强平时仍有 buffer，不会"突然资不抵债" |
| 财富统计 / sweep | LCV | 内部分析保守一致 |

## 代码入口

后端：
- `services/wealth.py::compute_users_holdings_value()` → LCV
- `services/wealth.py::compute_users_holdings_value_mtm()` → MTM
- `api/v1/user.py::get_user_summary()` → 同时返回两套
- `api/v1/loan.py::_holdings_value()` → LCV（薄封装，统一走 services.wealth）
- `api/v1/market.py::leaderboard()` → MTM

前端：
- `types/user.ts::UserSummary` 同时含 `holdings_value` (MTM) + `holdings_value_liquidation` (LCV)
- `components/user/MarginStatusCard.vue` 公式拆解明确用 LCV，标注 MTM 差距让用户看到滑点

## 维护规则

**新增涉及估值的接口/查询时，必须明确口径**：

- 显示给玩家看的 → MTM
- 影响资金/风控（借款额度、强平、margin） → LCV
- 不确定的 → 默认 LCV（保守），并在 PR 描述里说明理由

**绝不要**：
- 把 MTM 和 LCV 算出的值混在同一个公式里
- 在 hot path（BUY/SELL）里调用这两个函数（它们是 N+1 query 的批量场景函数）

## 历史背景

最初只有 LCV 一个口径（fix 1 of `feat/forced-liquidation` 统一过）。但是发现：
1. `api/v1/loan.py` 一直偷偷用 MTM 算 `_holdings_value`（手写 `amount × get_current_price`），
   导致 Loan 页面 NW ≠ Portfolio NW 的分裂体感
2. 大仓位玩家持续抱怨"NW 显示比账面少很多"

解决方案 = 双口径分工。本设计在 `feat/margin-status-display` 分支实现。
