# 贷款系统 V1 设计（LoanV1 — 复利 + APS sweep）

**日期**：2026-04-24
**范围**：给 TouhouCCB 加入"借款—复利—还款"玩法
**不在范围内**：保证金 / 强制平仓 / 多笔并行贷款 / 利率分层（留给 LoanV2）

---

## 1. 背景与目标

TouhouCCB 是基于 LMSR 的预测市场小游戏。`User` 表已经预留了 `cash` 和 `debt` 两个 `Decimal(16,6)` 字段（含 `debt >= 0` 约束），净值计算 `cash - debt + holdings_value` 和排行榜排序 `cash - debt` 都已上线，但**没有任何借/还的 API 和 UI**。本设计填上这块业务逻辑。

**目标**：
- 玩家可以主动借款放大资金规模，代价是随时间复利增长的债务
- 管理员可以运行时调整额度倍数、利率、结算频率、总开关，无需改代码
- 管理员可以对特定用户强制放贷或免除债务
- 为将来的"保证金 / 强平"（LoanV2）留出平滑升级路径

**明确不做的事**：
- 不做多笔并行贷款（单笔欠款字段即可）
- 不做本金 / 利息分家（采用复利后它们不再需要分）
- 不做强制平仓（靠用户自己卖持仓还债）
- 不做贷款历史流水（复利语义下流水无业务意义，审计靠日志）
- 不做利率分层 / 不同用户不同利率

---

## 2. 数据模型

### 2.1 `User` 表新增字段

| 字段 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `debt_last_accrued_at` | `datetime(tz=UTC)` nullable | `NULL` | 最近一次利息结算时间；`debt = 0` 时为 `NULL` |

**不新增**：本金字段、利率快照字段——复利把两者都消除了。

### 2.2 新表 `SiteConfig`（key-value）

```
id              int pk
key             varchar(64) unique, index
value           text         -- 统一以字符串存，读时按 key 约定解析
value_type      varchar(16)  -- "decimal" | "int" | "bool"（仅提示用）
updated_at      datetime(tz=UTC)
updated_by      int fk user.id nullable
```

**初始 keys**：

| key | value_type | 默认 | 含义 |
|---|---|---|---|
| `loan_enabled` | bool | `true` | 总开关。`false` 时禁止新增借款，但旧债仍计息 |
| `loan_leverage_k` | decimal | `1.0` | `最大可借 = k × 当前净值 − 当前债务` |
| `loan_daily_rate` | decimal | `0.01` | 日利率，复利。0.01 = 每天 1% |
| `loan_sweep_interval_sec` | int | `60` | APS sweep 间隔秒数，运行时可改 |

### 2.3 迁移

红线写明"没有迁移机制，`create_all` 不改已有列"。必须提供幂等迁移脚本：

`backend/scripts/migrate_loan_v1.py`
- `ALTER TABLE "user" ADD COLUMN IF NOT EXISTS debt_last_accrued_at TIMESTAMPTZ NULL`
- `CREATE TABLE IF NOT EXISTS siteconfig (...)` + unique index on `key`
- 插入四条默认配置（`ON CONFLICT (key) DO NOTHING`）
- 兜底：`UPDATE "user" SET debt_last_accrued_at = NOW() WHERE debt > 0 AND debt_last_accrued_at IS NULL`（实际线上应无此类数据，防御性写入）

部署文档（`docs/deploy.md`）追加一节说明"LoanV1 上线步骤"。

---

## 3. 利息结算：APS sweep + 交易兜底

### 3.1 为何选 APS 定时 sweep（而非读时即算 / 小时复利定时任务）

- 项目规模：有债用户预计 < 100，1 分钟一次全扫描成本可忽略
- 读侧逻辑零成本：`debt` 字段本身 ≈ 实时，`/user/me` / 排行榜 / 持仓接口直接读字段
- "躺平不操作"问题自动消失：sweep 直接动字段，用户感知上连续（每分钟跳一次，增量极小）
- 未来升级 LoanV2：同一个 sweep 内加强平检查即可，频率从 60s 提到合理值就是强平延迟

### 3.2 Sweep 逻辑

`backend/app/services/loan_sweep.py`，FastAPI `startup` event 注册 APScheduler `IntervalJob`：

```python
async def sweep_tick():
    rate = site_config.get_decimal("loan_daily_rate")
    if rate <= 0:
        return
    async with session() as db:
        rows = await db.exec(
            select(User.id).where(User.debt > 0)
        )
        for uid in rows:
            async with db.begin():
                u = await db.exec(
                    select(User).where(User.id == uid).with_for_update()
                ).one()
                _accrue(u, rate, now_utc())
                db.add(u)

def _accrue(u: User, daily_rate: Decimal, now: datetime) -> None:
    if u.debt <= 0 or u.debt_last_accrued_at is None:
        return
    elapsed = (now - u.debt_last_accrued_at).total_seconds()
    if elapsed <= 0:
        return
    factor = Decimal(1) + daily_rate * Decimal(elapsed) / Decimal(86400)
    u.debt = (u.debt * factor).quantize(Decimal("0.000001"))
    u.debt_last_accrued_at = now
```

**关键点**：
- 用 `elapsed` 而非假设固定 60s → scheduler 漏跑 / 容器重建后下次补齐，不丢息
- `SELECT ... FOR UPDATE` 每一行，和交易路径（`market.py:55` 锁 User 行）使用同一把锁
- 精度：`quantize(Decimal("0.000001"))` 保持 6 位小数与 `cash` 一致
- 不写任何流水表（避免每分钟/人一行）；sweep 级别事件用 `logger.debug`，每天 0 点打一条 `INFO` 汇总（"今日共结息 N 户，总增长 X"）

### 3.3 间隔运行时可调

启动时从 `SiteConfig` 读 `loan_sweep_interval_sec` 注册 job。管理员改这个值时：
- 配置写库后，`reschedule_sweep()` 从 scheduler 里 remove 旧 job、注册新 job
- 允许的值范围：[10, 3600]，超出报 400

### 3.4 交易侧兜底 `accrue_in_txn(u)`

所有会改 `cash` / `debt` 的路径（`buy` / `sell` / `borrow` / `repay` / 管理员 `adjust-cash` / 管理员 `force-loan`），在 `SELECT ... FOR UPDATE` 之后、业务逻辑之前先调一次 `_accrue(u, rate, now_utc())`。保证时点偏差 ≤ 一次交易的延迟。

---

## 4. API

### 4.1 玩家接口（限速 5r/s，沿用 `/market/*` 中间件）

#### `GET /api/v1/loan/quota`
返回当前可借额度和状态。
```json
{
  "enabled": true,
  "cash": "1000.00",
  "debt": "0.00",
  "net_worth": "1500.00",
  "leverage_k": "1.0",
  "daily_rate": "0.01",
  "max_borrow": "1500.00",    // = k × net_worth − debt，下限 0
  "last_accrued_at": null
}
```

#### `POST /api/v1/loan/borrow`
Body: `{"amount": "200.00"}`
- 若 `loan_enabled == false` → 403
- 若 `amount ≤ 0` → 400
- `SELECT ... FOR UPDATE user` → `_accrue` → 校验 `debt + amount ≤ k × net_worth`（净值用 `cash + holdings_value − debt`，和 `/user/me` 同源）
- 不满足 → 400，返回当前 `max_borrow`
- 通过：`cash += amount`，`debt += amount`，若 `debt_last_accrued_at is None` 则设 `now`
- 返回新的 `cash` / `debt` / `max_borrow`
- 打 `INFO` 日志：`LOAN_BORROW user_id=.. amount=.. new_cash=.. new_debt=..`

#### `POST /api/v1/loan/repay`
Body: `{"amount": "50.00"}`
- 若 `amount ≤ 0` → 400
- 若 `amount > cash` → 400 `"还款额超过现金余额"`
- `SELECT ... FOR UPDATE user` → `_accrue` → `effective = min(amount, debt)`
- `cash -= effective`，`debt -= effective`
- 若 `debt == 0`：`debt_last_accrued_at = None`
- 返回新的 `cash` / `debt`
- 打 `INFO` 日志：`LOAN_REPAY user_id=.. requested=.. effective=.. new_cash=.. new_debt=..`

> 备注：请求额 > debt 时**静默扣到 0**（不报错、不退差），避免前端输入"全还"时的边界处理。

### 4.2 管理员接口（超管限速 2r/s）

#### `GET /api/v1/admin/site-config`
返回全部 SiteConfig，每项包含 `key` / `value` / `value_type` / `updated_at` / `updated_by`。

#### `PUT /api/v1/admin/site-config/{key}`
Body: `{"value": "1.5"}`
- key 必须在白名单（当前四个 loan_ 开头）
- value 按 `value_type` 校验（decimal 正数、int 范围、bool 字面）
- `loan_sweep_interval_sec` 改后立即 `reschedule_sweep()`
- 打 `INFO` 日志：`SITECONFIG_SET admin_id=.. key=.. old=.. new=..`

#### `POST /api/v1/admin/user/{user_id}/force-loan`
Body: `{"amount": "500.00", "reason": "活动奖励 / 惩罚"}`
- `amount > 0`
- 不受 `loan_leverage_k` 约束（管理员免校验）
- **仍受 `loan_enabled` 约束**（关掉就是关掉，管理员也要遵守，避免"以为关了实际没关"的运维事故。如需绕过则先打开开关再关回去）
- `_accrue` → `cash += amount`、`debt += amount`、`debt_last_accrued_at = now if None`
- 日志：`FORCE_LOAN admin_id=.. user_id=.. amount=.. reason=..`

#### `POST /api/v1/admin/user/{user_id}/forgive-debt`
Body: `{"amount": "100.00", "reason": "..."}`
- `amount > 0`
- `_accrue` → `effective = min(amount, debt)`；`debt -= effective`；`cash` 不变
- `debt == 0` 时清 `last_accrued_at`
- 日志：`FORGIVE_DEBT admin_id=.. user_id=.. requested=.. effective=.. reason=..`

---

## 5. 前端（均在安全区）

### 5.1 新页 `frontend/src/pages/loan.vue`

工业风黑白、粗边框、无圆角（参见 `docs/style.md`）。布局：

- 顶部：当前债务大字 + 最近结息时间
- 中部两块卡片：
  - **借款**：输入金额（不超过 max_borrow），按钮"借入"
  - **还款**：输入金额，按钮"还款"，下方"全部还清"快捷
- 底部：当前参数（日利率、杠杆倍数、下次 sweep 预计时间）灰字
- 债务增长 = 红色数字；还款 = 绿色数字（和涨跌色一致）

### 5.2 Header 钱包区增强

`components/` 里 Header 现金显示旁，若 `debt > 0` 出现红色徽章 `负债 X`，点击跳 `/loan`。

### 5.3 管理员 `/admin` 新增区块

三块：
1. **站点配置表**：四项 loan_ 配置一行一项，就地编辑
2. **强制放贷**：选用户 + 金额 + 原因
3. **免除债务**：选用户 + 金额 + 原因

复用已有的 admin 面板风格。

### 5.4 路由

`router/` 里 push 一条：`/loan → pages/loan.vue`。不改 `router/` 其他文件、不改 `stores/` 既有 store（新建 `stores/loan.ts` 只管 loan 页的本地状态）。

---

## 6. 测试

### 6.1 后端 `backend/tests/test_loan.py`

必须覆盖：
- **额度边界**：`debt + amount == k × net_worth` 通过；超 1 微分 400
- **还款溢出**：`repay(amount > debt)` 扣到 0 不报错
- **还款超现金**：`repay(amount > cash)` 400
- **复利精度**：构造 `debt=1000, rate=0.01`，手动 sweep 10 次（模拟 10 分钟，`elapsed=60s each`），断言结果与 `1000 × (1 + 0.01×60/86400)^10` 误差 < `1e-5`
- **漏跑补齐**：手动把 `last_accrued_at` 回拨 1 小时，一次 sweep 应补满这 1 小时的利息
- **loan_enabled=false**：borrow / force-loan 均 403，但 sweep 继续工作
- **并发**：两个 borrow 并发，一个应当因 `FOR UPDATE` 排队；总借入额不超过 quota
- **管理员接口鉴权**：非超管 403
- **sweep reschedule**：改 `loan_sweep_interval_sec` 后 scheduler job 的 interval 变化

### 6.2 前端手测（`CLAUDE.md` 要求）

浏览器实测主路径 + 边界：
- 借 100 → cash / debt 都 +100
- 等 1-2 分钟看 debt 是否涨
- 还 50 → cash / debt 都 -50
- 还 999999（远超 debt）→ 扣到 0、cash 扣对应 debt 量
- `loan_enabled=false` → 借/还页按钮禁用或 403 toast
- 移动端页面不错位、管理员面板权限路径正常

---

## 7. 安全与红线检查

- **不碰 `lmsr.py` / `realtime.py` / `market.py` 的定价与 SSE 主逻辑**，只在 `market.py` 交易路径前插入一行 `_accrue(u, ...)`
- **不改 `base.py` 已有字段**，只新增字段 / 新建表，并配套迁移脚本
- **不改 `auth.py` / `admin.py` 的鉴权逻辑**，新接口复用现有 `require_admin` 依赖
- **不用 `-A` / `.` 提交**，按文件 add
- **不在 `main` 直接 commit**，新分支 `ralph/2026-04-24-loan`
- **不 push**（push 即部署）
- **遇到要跑迁移脚本时停下问用户**（影响生产 DB）

---

## 8. 风险与回滚

| 风险 | 影响 | 缓解 |
|---|---|---|
| sweep 精度累积漂移 | 长期债务金额偏差 | 每步 `quantize(0.000001)`；每步使用 `(1 + rate × dt)` 线性近似（本质仍是复利：每次作用在当前债务上），避免 `Decimal` 指数运算的精度损失 |
| APS job 漏跑 | 利息延迟计入 | `elapsed` 驱动，不假设固定间隔 |
| 并发借款穿透额度 | 某用户 debt 超 k×net_worth | `SELECT FOR UPDATE` 用户行 |
| 管理员误调利率到极端值 | 全站债务爆炸 | `PUT /site-config` 校验合理范围（`daily_rate ∈ (0, 1)`），UI 加二次确认 |
| 迁移脚本失败 | 部署中断 | 脚本幂等，失败可重跑；先在本地/staging 验证 |

**回滚**：
- 代码层面：revert 分支 merge commit
- 数据层面：`debt_last_accrued_at` 列保留不动（无害）；SiteConfig 表保留；所有已借出的 debt 仍计入 `net_worth = cash - debt`，排行榜正确；借/还接口失效即等效"冻结"

---

## 9. 决策记录（供 LoanV2 参考）

- **复利 vs 单利**：选复利，因为不需要记本金/利息快照，字段更少
- **sweep vs 读时即算**：选 sweep，因为解决"躺平不操作"感知问题 + 为强平铺路
- **单笔 vs 多笔**：选单笔，足够游戏场景
- **利率分层**：否决，统一利率
- **强平**：V1 不做，V2 在同一 sweep 里加 `net_worth < maintenance_margin` 检查

---

## 10. 下一步

本 spec 获批后进入 `writing-plans` skill，产出分阶段实施计划（建议顺序：迁移脚本 → SiteConfig 服务 → loan sweep + accrue → 玩家 API + 测试 → 管理员 API + 测试 → 前端 loan 页 → Header 徽章 → 管理员面板）。
