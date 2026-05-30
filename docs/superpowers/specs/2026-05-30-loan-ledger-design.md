# 资金流水账本（LedgerEntry）设计 spec

> **Status**: Approved（设计阶段）
> **Owner**: Renko6626
> **Created**: 2026-05-30
> **Related**:
> - `docs/holdings-value-semantics.md`（MTM/LCV 双口径）
> - `docs/migrations.md`（schema 变更走 alembic）
> - `backend/app/services/loan_service.py`（借/还/利息核心）

## 背景与动机

做离线数据分析时发现一个**数据缺口**：用户 `cash`/`debt` 的部分变动**没有任何历史流水**，导致无法精确重建任意历史时刻的净值（净值 = cash + 持仓估值 − debt）。

盘点全站改动 `cash`/`debt` 的事件：

**已有流水（不动）**
| 事件 | 现有记录 |
|---|---|
| 买 / 卖 / 结算 | `Transaction` |
| 强平 | `LiquidationEvent` + LIQUIDATE `Transaction` |
| 兑换码购买 | `RedemptionTransaction` |
| 弹幕兑换 | `DanmukuExchange` |

**无流水（本 spec 要补）**
| 事件 | 代码位置 | cash | debt |
|---|---|---|---|
| 借款 | `loan_service.increase_debt` | ↑ | ↑ |
| 还款 | `loan_service.decrease_debt_locked` | ↓ | ↓ |
| 利息累计 | `loan_service.accrue_interest`（`loan_sweep` 每 N 秒跑全体负债用户） | — | ↑ |
| 管理员调现金 | `user.py` adjust-cash | ± | — |
| 管理员强制放贷 | `user.py` force-loan | ↑ | ↑ |
| 管理员免债 | `user.py` forgive-debt | — | ↓ |

> 教训记录：动态净值/排行榜的历史曲线因这个缺口**已无法回溯重建**（旧应用日志随 `docker compose down` 丢失，json-file 日志只滚动保留 30MB 且无外部收集）。本功能保证**未来**的数据可精确重建。

## Goal

给上述**无流水的外部资金事件**补一张账本表 `LedgerEntry`，使未来任意时刻的 `cash`/`debt` 可被精确重建。

**非目标（明确不做）**
- 不动 buy/sell hot path（已有 `Transaction`，不重复记账；也避免触碰性能护栏）。
- **利息不单独落行**——它是确定性可推导的（`debt × (1 + daily_rate)^(Δt)`，有时间锚点、有当时利率），分析时按"上一笔事件 → 下一笔事件"用公式补算即可。落行会导致行数随 用户数 × sweep 频率 爆炸。
- 不做动态净值排行榜（已放弃）。
- 不改 `User` 表结构。

## 设计

### 核心思路

只给**无法推导的外部输入事件**（借、还、管理员三种）落流水，每行携带**操作后的 cash/debt 快照**作为重建锚点，并记录**当时的利率**让分析侧能精确补算两次事件之间的利息。

利息本身不落行。重建某用户净值曲线时：取其全部 `LedgerEntry` 按时间排序，每个相邻区间用 `daily_rate_at_event` 和区间时长按复利公式补算债务增长，事件点用快照校准。

### 新表 `LedgerEntry`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | PK | |
| `user_id` | int, FK→user.id, index | 资金变动归属用户 |
| `entry_type` | str(32) | `borrow` / `repay` / `admin_adjust_cash` / `admin_force_loan` / `admin_forgive_debt` |
| `cash_delta` | Numeric(16,6) | 本次 cash 变动（+/−，可为 0） |
| `debt_delta` | Numeric(16,6) | 本次 debt 变动（+/−，可为 0） |
| `cash_after` | Numeric(16,6) | **操作后** cash 快照（重建锚点） |
| `debt_after` | Numeric(16,6) | **操作后** debt 快照（重建锚点） |
| `debt_last_accrued_at_after` | datetime(tz) nullable | 操作后的结息锚点（利息重算起点；debt=0 时为 null，与 User 字段语义一致） |
| `daily_rate_at_event` | Numeric(16,8) nullable | 事件发生时的 `loan_daily_rate`（利率会被 admin 改，必须记当时值） |
| `operator_user_id` | int, FK→user.id, nullable | 管理员操作时记是哪个管理员；用户自助操作为 null |
| `reason` | str(200) nullable | 管理员调账备注（adjust-cash / force-loan / forgive-debt 已有 reason 入参） |
| `created_at` | datetime(tz), index | 事件时间 |

**约束 / 索引**
- `user_id` index、`created_at` index（按用户重建、按时间聚合都要）。
- `entry_type` 不加 DB 级 enum 约束（沿用项目惯例：`Transaction.type`、`LiquidationEvent.trigger_source` 都是裸 str + 应用层取值），但在代码里用常量集合收敛。
- 精度：金额 `Numeric(16,6)`、利率 `Numeric(16,8)`，与全站 Decimal 精度约定一致。

### 写入点（全部在原有事务内同步写，均非 hot path）

| 写入点 | 文件 | entry_type | 备注 |
|---|---|---|---|
| 借款 | `loan_service.increase_debt` | `borrow`（用户）/ `admin_force_loan`（管理员） | 需区分调用来源 |
| 还款 | `loan_service.decrease_debt_locked` | `repay`（用户）/ `admin_forgive_debt`（管理员免债） | 需区分调用来源 |
| 管理员调现金 | `user.py` adjust-cash handler | `admin_adjust_cash` | 带 reason、operator |

**来源区分**：`increase_debt` / `decrease_debt_locked` 现被用户借还 **和** 管理员 force-loan/forgive-debt 共用。为正确打 `entry_type` 和 `operator_user_id`，给这两个函数加参数（如 `source: str` 和 `operator_user_id: Optional[int]`），由调用方传入。这样账本写入逻辑收敛在 loan_service 一处，避免散落。

> `forgive-debt` 走 decrease_debt（consume_cash=False，只减 debt 不动 cash），`entry_type=admin_forgive_debt`；普通 `repay` 走 decrease_debt（consume_cash=True），`entry_type=repay`。来源参数同时承载这个区分。

**快照来源**：写 ledger 行时，`cash_after`/`debt_after`/`debt_last_accrued_at_after` 取**已 accrue + 已增减后**的 user 对象当前值；`daily_rate_at_event` 取本次操作传入的 `daily_rate`。

### 重建算法（分析侧，不在本仓库实现，仅说明数据足够）

给定某 user 的 `LedgerEntry` 列表（按 `created_at` 升序）+ 起始状态：
1. 每个事件点：`cash`/`debt` 直接取该行快照（精确）。
2. 相邻两事件之间：`debt(t) = debt_after_prev × (1 + daily_rate_at_event_prev × Δseconds / 86400)`（与 `accrue_interest` 同公式）；`cash` 在区间内不变（区间内无外部事件）。
3. 结合持仓重放（来自 `Transaction`）即得净值曲线。

> 注意：buy/sell/settle/强平/兑换/弹幕引起的 cash 变动**不在 LedgerEntry 里**，它们在各自的表。完整 cash 重建需 union LedgerEntry + 这些表。本表只补"原本完全无记录"的那部分。

### Schema 变更流程

- 纯新增表，不改 `User`，风险低。
- **必须走 alembic**：`alembic revision --autogenerate -m "add ledger_entry table"` → 人工 review 生成的 migration → 本地 `alembic upgrade head` 验证 → 模型 + migration 一起进同一 commit。
- 模型加在 `backend/app/models/`（新建 `ledger.py` 或并入 `base.py`，实现时定；倾向新文件 `ledger.py` 保持 base.py 聚焦）。

## 影响面与风险

- **高敏感**：触碰 `loan_service.py`（资金核心）和 `user.py` 管理员接口。改动是"在既有事务里多 insert 一行 + 给两个函数加参数"，不改既有金额计算逻辑。
- **事务一致性**：ledger 写入与 cash/debt 变动在**同一事务**，要么都成功要么都回滚，不会出现"钱变了但没流水 / 有流水但钱没变"。
- **不碰 hot path**：buy/sell 完全不动。
- **测试**：每个写入点加单测（借/还/管理员三类各验证 ledger 行被正确写入、快照值正确、来源/operator 正确）；复用现有 loan/admin 测试夹具。
- **回填**：历史已发生的借还/调账**无法回填**（无源数据），账本从上线时刻开始有效。这点在实现里和 README 注明。

## 验收标准

- [ ] `LedgerEntry` 表经 alembic migration 建立，本地 upgrade/downgrade 通过。
- [ ] 借款 / 还款 / 管理员调现金 / 强制放贷 / 免债 五类操作各写入正确的 ledger 行（类型、delta、快照、利率、operator、reason 正确）。
- [ ] ledger 写入与资金变动同事务（注入失败时一起回滚）。
- [ ] 既有 loan/admin 行为与返回值不变（现有测试全绿）。
- [ ] 后端验证全过：`py_compile` + `import app.main` + `pytest -x`。
