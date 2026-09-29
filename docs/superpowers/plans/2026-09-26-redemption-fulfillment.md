# 管理员线下核销 Implementation Plan

> **For agentic workers:** Use the approved in-chat design and execute the scoped backend and frontend tasks. Preserve other working-tree edits.

**Goal:** 管理员查看、搜索兑换码并确认线下核销，记录操作人和时间，阻止重复发放。

**Architecture:** 保留库存 available/sold 和用户个人已用备注；增加独立的管理员核销字段。管理员接口提供分页列表、核销和有原因的撤销；数据库条件更新保证并发只成功一次，审计事件与状态同事务写入。

**Tech Stack:** FastAPI / SQLModel / SQLAlchemy / Alembic / Vue 3 / TypeScript / existing Naive UI.

**Spec:** 用户已同意上一轮设计，并指定“工作人员也是管理员，查看所有可用兑换码列表，搜索后点按钮核销”。

## Global Constraints

- 只用现有管理员权限，不创建新的人员角色。
- 用户明确授权管理员查看所有兑换码；普通用户仍只能查看自己购买的码。
- 未售出的码可查看，只有 sold 且有购买人的未核销码能核销。
- 保留用户个人标记，不得覆盖或取消管理员核销记录。
- 不修改资金、库存销售状态；核销和撤销均记录时间、操作人、备注/原因。
- 不新增依赖、不放宽 TypeScript、不修改部署或生产数据库。
- 保留工作区已有配置、首页、宣传页和未合并文档的改动。

## Contract

- GET `/api/v1/admin/redemption/codes`: params `batch_id?: number`, `q?: string`, `status=all|available|pending|redeemed`, `page=1`, `page_size=50` (max 200); returns `{items,total,page,page_size}`.
- Item: `id`, `batch_id`, `batch_name`, `partner_name`, `code_string`, `status: available|sold`, `bought_by_user_id: number|null`, `bought_by_username: string|null`, `bought_at: string|null`, `redeemed_at: string|null`, `redeemed_by_admin_id: number|null`, `redeemed_by_admin_username: string|null`, `redemption_note: string`.
- POST `/api/v1/admin/redemption/codes/{id}/redeem`, JSON `{note?: string}` (max 500), returns Item. Unknown id 404, unsold/already redeemed 409.
- POST `/api/v1/admin/redemption/codes/{id}/revoke`, JSON `{reason: string, expected_redeemed_at: string}` (reason 1–500, whitespace rejected; timestamp from the opened confirmation), returns Item. Unknown id 404, not redeemed / stale confirmation 409.
- `MyRedemptionItem` adds `redeemed_at: string|null`; ordinary user sees authoritative “线下已兑换” and timestamp, keeps personal “标记已用” under explicit personal-note wording.
- `BatchAdminItem` adds `redeemed_count: number`.
- Frontend route `/admin/redemption/codes` with optional `?batch_id=…`; enter from sidebar and batch row “查看 / 核销”.

## Review Focus

- 同一码重复请求 / 两个管理员并发：只一次成功，只一条核销审计。
- 用户取消个人标记：管理员核销仍有效。
- 搜索包含 `_`、`%`：按字面匹配，不把它们当通配符。
- 分页切换和搜索重置：请求晚到不得覆盖新结果。
- 旧数据库已有库存：迁移保留行，新增字段默认未核销，升级和回退可执行。

## Task 1: 后端核销与迁移（主代理）

- [x] 新增 API 行为测试，先运行确认功能缺失导致失败。
- [x] 增加 RedemptionCode 三个字段、响应类型和管理员核销审计事件类型。
- [x] 实现列表过滤、分页、权限、原子核销和撤销。
- [x] 用户响应增加核销时间；批次统计增加核销数。
- [x] 在临时 SQLite 旧模型快照上生成 Alembic autogenerate 迁移，验证数据保留和回退；不连接生产库。
- [x] 运行相关测试和完整后端套件，记录既有失败。

## Task 2: 管理员界面及用户状态（前端代理）

- [x] 扩展类型与 API，添加管理员路由和侧栏入口，保留宣传页路由。
- [x] 实现列表搜索、批次/状态筛选、分页、刷新、核销确认、备注、撤销原因，以及加载/错误/空状态。
- [x] 批次页增加入口与已核销数量；用户页区分个人标记与管理员确认。
- [x] 核销期间禁用重复点击，成功/409 后刷新服务器状态，不根据本地标志宣布成功。
- [x] 运行 type-check 和修改文件 lint。

## Integration

- [x] 浏览器验证桌面/移动端列表、搜索、核销、重复保护、权限和错误状态。
- [x] 前端构建、单测、完整 lint（仅报告旧问题，不自动改无关文件）。
- [x] 请求代码审查并修正重要问题。
- [x] 报告实现与验证、分支及待部署迁移；不推送、不部署。

## Final evidence

- 核销/API/迁移相关测试：31 passed, 1 skipped。
- 完整后端：582 passed, 1 skipped, 3 个既有余额默认值测试失败（详见 docs/redemption-fulfillment.md）。
- 前端 type-check/build/44 项单测通过，改动文件 lint 通过；全仓库 59 个旧 lint 错误保留。
- 真实本地 API 浏览器主流程、移动端及旧核销/撤销弹窗冲突均验证通过。
- 代码审查修正旧撤销确认版本问题后复核通过。
- 分支 ralph/2026-09-26-redemption-fulfillment，未 commit/push，未部署或迁移生产库。

## 发布结果（2026-09-27）

用户授权直接提交、推送并自动部署。使用独立发布工作区 `/tmp/sunyunbo/touhouccb-fulfillment-release`，保留原工作区已有暂存内容和未合并文档。正式发布版本完整后端测试 585 passed, 1 skipped，前端类型检查、构建和 44 项单测通过。

提交 `a92e76a2b3c86a164d61d80165b38379b2c2f0dd` 已推送 `origin/main`。GitHub Actions 36259992560 全部成功；部署日志确认自动迁移 `ba3a85c3b675 -> 0d0ac23efa85`，部署与远程健康检查均通过。未手动升级生产数据库。
