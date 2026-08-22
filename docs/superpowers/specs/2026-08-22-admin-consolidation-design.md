# 管理员功能整合 + 「大赦天下」批量重置 — 设计

日期：2026-08-22　分支：`refactor/2026-08-22-admin-consolidation`

## 动机

管理员对「用户 / 资金 / 贷款 / 账号」的操作散落在 `user.py`（`/api/v1/user/*`）、
`admin_title.py`（`/admin/users/{id}/summary`）、`site_config.py` 三处后端文件，
逻辑全部写在路由函数里；前端同一操作在 `UserManage` / `MarketManage` /
`SiteConfig` / `BotReviewBan` / `BatchAdjustCash` 五个页面重复出现，侧栏 10 个
平铺入口。要加「大赦天下」（批量清债 + 现金还原到初始）时没有合适的落点。

## 后端

### 新增 `app/services/admin_user_service.py`

纯业务函数，路由层只做鉴权 + 参数校验 + 调用 + 日志：

- `adjust_cash(db, target_id, amount, reason, admin_id)`
- `force_loan(db, target_id, amount, reason, admin_id)`
- `forgive_debt(db, target_id, amount, reason, admin_id)`
- `set_role(db, target_id, is_admin, admin_id)`
- `ban(db, target_id, reason, suspicion_id, admin_id)` / `unban(...)`
- `build_user_filter_stmt(filter)`（从 user.py 搬）
- `batch_adjust_cash(db, filter, amount, reason, admin_id, dry_run)`
- `amnesty(db, filter, reset_cash_to, forgive_debt, reason, admin_id, dry_run)`

### 新增 `app/api/v1/admin_users.py`，挂载 `/api/v1/admin/users`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 用户列表（原 `user/list`） |
| GET | `/{id}` | 用户快照（原 `admin/users/{id}/summary`）|
| POST | `/{id}/cash` | 调现金（原 `user/{id}/adjust-cash`）|
| POST | `/{id}/loan` | 强制放贷（原 `user/{id}/force-loan`）|
| POST | `/{id}/forgive-debt` | 免债 |
| PATCH | `/{id}/role` | 设/取消管理员（原 `user/{id}/admin`）|
| PATCH | `/{id}/ban` `/{id}/unban` | 封/解封 |
| POST | `/batch/adjust-cash` | 批量调现金（原 `user/batch-adjust-cash`）|
| POST | `/batch/amnesty` | **新** 大赦天下 |

请求/响应体与原接口保持一致（前端改路径即可）。旧路径**直接删除**，不留兼容
别名（单人项目，`quant/` `loadtest/` 已确认未引用）。`user.py` 只剩普通用户接口。

### 大赦天下语义

```
AmnestyRequest:
  filter: BatchAdjustCashFilter   # 同批量调现金，默认排除超管
  reset_cash_to: Decimal | None   # None → site_config.initial_balance
  forgive_debt: bool = True
  reason: str (必填)
  dry_run: bool = True
```

对每个匹配用户（单事务，FOR UPDATE 按 id ASC 锁，hardcap 500）：
1. 若 `forgive_debt` 且 `debt>0`：`loan_service.decrease_debt_locked(consume_cash=False)`
   先结息再清零（effective = 结息后全部债务）
2. `cash_delta = reset_cash_to - cash`（可正可负），`cash = reset_cash_to`
3. 写**一条** `LedgerEntry(entry_type="admin_amnesty", cash_delta, debt_delta=-effective)`
4. 持仓不动（方案 1，UI 上明示）

dry_run 返回每人 `cash_before/cash_after/debt_before/debt_after`、
`total_cash_delta`、`total_debt_forgiven`。`LEDGER_ENTRY_TYPES` 增加 `admin_amnesty`。

## 前端

### 侧栏分组（复用 `NavGroup`）

```
管理
├ 用户与资金：用户管理 /admin/users ｜ 批量操作 /admin/users/batch
├ 市场管理   /admin/markets
├ 风控：Bot 预警 /admin/bot ｜ 资产统计 /admin/wealth-stats
├ 兑换与称号：合作方 / 兑换批次 / 称号目录 / 称号激活码（不动）
└ 站点配置   /admin/site-config
```

### 页面

- `UserManage.vue` 重做：左列表（搜索）+ 右详情，tab：资产（调现金）/ 贷款
  （强制放贷、免债）/ 账号（封禁、管理员）/ 称号。所有动作原因必填 + 确认。
- `BatchOps.vue`（新）：tab「批量发钱」= 原 BatchAdjustCash；tab「大赦天下」=
  filter + 目标现金（预填 initial_balance）+ 是否免债 + 原因 → dry-run 预览 →
  输入「大赦天下」确认 → 结果。
- `MarketManage.vue` 删掉调现金 / 设管理员段；`SiteConfig.vue` 删掉放贷/免债段；
  `BotReviewBan.vue` 删掉手动封号段（已封列表保留，跳转用户管理）。
- `api/admin.ts`：`adminUsersApi` 统一；删 `loan.ts` 的 `adminSiteConfigApi`。
- 强平 `run-now` 按钮放进资产统计页（此前无 UI）。

## 验证

后端：现有 `test_batch_adjust_cash / test_loan_admin / test_set_admin /
test_ledger_adjust_cash / test_title_admin_user` 改路径；新增 `test_admin_amnesty.py`
（dry-run 不落库、结息后清债、cash 高于目标也降、默认排除超管、hardcap、
一人一条 ledger、非管理员 403）。`pytest -x` 全绿。
前端：`type-check` + `lint` + `build`，VNC 浏览器实测各 tab 主路径与空态。
