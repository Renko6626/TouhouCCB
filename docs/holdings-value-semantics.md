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

## HALT 市场的不对称处理

> **2026-08-22 起，「TRADING 但已过 `closes_at`」的市场在 LCV / 强平 / 强平豁免三处与 HALT 同等对待**
> （统一判定在 `app/services/market_open.py::market_is_open`）。理由：过期市场用户已不能买卖，
> 若仍算作可变现抵押、且强平还能卖，就出现「用户不能自救却能被强卖」的不对称。MTM 不受影响。

两套口径对非 TRADING 市场的持仓**有意采用不同处理**：

| 口径 | HALT 持仓 | 理由 |
|---|---|---|
| **MTM** | **正常计入**（按瞬时价算） | "账面估值" 不关心能否变现。避免临时 HALT 让用户账面归零、体感像爆仓 |
| **LCV** | **不计入**（按 0 算） | "立即变现" 真的拿不到。计入会让 margin 被 HALT 资产虚撑，借款页面虚高 collateral |

**典型场景**：用户全押 outcome A，market 进入 HALT
- Portfolio 顶部"净资产"显示 ≈ 投入额（MTM 不变）→ 用户不慌
- MarginStatusCard 显示 LCV NW ≈ cash → margin 暂时算"无抵押"，借不到新钱
- 用户主动 sell 被市场拒（HALT），但**不会被强平**（强平只处理 TRADING 市场）
- Market 恢复 TRADING 后，两套口径自动恢复一致

**强平动作本身（`liquidation_service.py:73-80`）**：
- 跳过所有非 TRADING 市场的持仓（保持原状不动）
- 跟 LCV 过滤一致：不可变现 → 不参与强平
- 只对 TRADING 持仓执行 sell + 还债

### 流动性危机保护 (`liquidation_sweep.py`)

**问题**：用户混合持有 TRADING + HALT 市场仓位时，HALT 部分 LCV 算 0 但
MTM 可能很大。盲目按 LCV margin 强平 → 用户的 TRADING 持仓被卖光，损失滑点
+ 失去后续机会，**实际总资产可能仍健康**。这是 LTCM 1998 经典流动性危机：
资产够，但短期变不了现就被强行平仓。

**守卫（双层）**：
- **阶段 1 预筛**：candidate query 通过 `NOT EXISTS` 子查询排除"有 HALT 持仓"
  的用户，避免无谓 `lock_user FOR UPDATE` + 跟正常 BUY/SELL 抢锁
- **阶段 2 守卫**（defense-in-depth）：lock user 之后再调
  `services.wealth.user_has_halt_holdings`，防止 market 在阶段 1 → 阶段 2 之间
  被 admin halt 的 race condition

**假设**：HALT 状态由 admin 控制，持续时间短（小时级）。不存在
用户故意持有 HALT 持仓豁免强平的滥用空间。如果将来 HALT 持续时间变长，
需重新评估这个守卫是否合理。

**对照测**：`test_sweep_skips_user_with_halt_holdings` + `test_sweep_triggers_user_with_only_trading_holdings`

## 代码入口

后端：
- `services/wealth.py::compute_users_holdings_value()` → LCV
- `services/wealth.py::compute_users_holdings_value_mtm()` → MTM
- `api/v1/user.py::get_user_summary()` → 仅当 `debt > 0` 时调 LCV 算 `margin_status`（阶段 3 起不再把 MTM/LCV 数值本身下发，见下节）
- `api/v1/loan.py::_holdings_value()` → LCV（薄封装，统一走 services.wealth）
- `api/v1/market.py::leaderboard()` → MTM

前端：
- `types/user.ts::UserSummary` 阶段 3 起不再含 `holdings_value` / `net_worth` 等派生字段，改用
  `utils/lmsr.ts` + `utils/valuation.ts` + `stores/user.ts` 的 priceContext 本地推算
- `components/user/MarginStatusCard.vue` 公式拆解明确用 LCV，标注 MTM 差距让用户看到滑点

## 谁在算：阶段 3 起的服务端/客户端分工

阶段 3（spec 2026-08-21 §6.4）把 `/user/summary`、`/user/holdings` 的估值列
（`holdings_value` / `net_worth` / `unrealized_pnl` / `rank` / `margin_ratio` 等）
整体下放到前端本地算，后端不再在这两个高频轮询端点里跑全仓 LMSR：

- **服务端仍算**（权威口径，`services/wealth.py` 不变）：
  - 强平 sweep 判定（`liquidation_sweep.py`）
  - `/admin/wealth`
  - `/market/leaderboard` 排序（MTM）
  - `/user/summary` 的 `margin_status`——且仅在 `debt > 0` 时才跑一次 LCV，
    无债用户零 LMSR 开销
- **客户端现算**（阶段 3 起，仅用于显示）：
  - `/user/summary`、`/user/holdings` 不再返回 MTM/LCV/净值/浮盈/rank，只给
    `cash`（6dp，客户端本地 apply 成交后的 cash 基线）、`positions`（数量/成本）、
    `rank_thresholds`（阈值表，供前端本地映射称号）
  - 前端 `utils/lmsr.ts`（闭式 LMSR 公式）+ `utils/valuation.ts` + `stores/user.ts`
    的 priceContext 本地推算 MTM/LCV/净值/浮盈/rank；HALT 语义与 `wealth.py` 镜像
    （MTM 照算不过滤 HALT；LCV 在 HALT 时不计入，且"立即变现浮盈" = -cost_basis）

客户端估值是**显示口径**（价格可能轻微陈旧于服务端最新成交、且不做 6dp 资金量化），
真正的权威判定——强平是否触发、借款额度是多少——永远走服务端 `services/wealth.py`
（spec §6.3）。前端页面若需要"绝对准确"的净值（如借款申请前的最终确认），应
直接调后端权威接口，不能只信本地估算。

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
