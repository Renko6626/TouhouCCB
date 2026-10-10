# FX Borrow Buy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 在真实 FX 做多页提供可报价、可安全重试的原子借款买入。
**Architecture:** 专用融资入口复用现货交易事务、统一信贷和贷款流水；融资参数持久化到 FxTrade。只读报价和成交共用交易后模拟，现金交易保持原路径。
**Tech Stack:** FastAPI / SQLAlchemy / SQLModel / PostgreSQL / SQLite / Vue / TypeScript。
**Spec:** `docs/superpowers/specs/2026-10-10-fx-borrow-buy-design.md`

## Global Constraints

- 不新增依赖、独立订单表或后台任务；金额 Decimal 6dp、请求金额字符串。
- 统一信贷公式、品种先于 User 的锁序、完整门闩发现及版本复检保持。
- 先查原成功结果再写借款，借款与买入同事务；新本金只从当前时点计息。
- 无债现金快路径不读全组合；融资报价 350ms 防抖，不随 SSE 每帧更新。
- 用户要求直接执行，无额外计划确认；保留原工作区无关文档；用户随后授权提交推送和自动部署，最终集成到最新 main。
- 验证仅运行相关检查、必要并发对照及最多截图，不自动交互。

## Review Focus

- 响应丢失后刷新页面沿用原编号，避免重复借款。
- 空头锁金不能承担自有现金投入，同 pair 欠币不能融资建多。
- 用户切换后旧响应不得清除新用户请求或展示他人回执。
- 旧金债含息且时间较远时新增本金不能追溯计息。
- 普通 spot 与融资同编号冲突不能跨用途回放。

### Task 1: 后端融资成交、报价与迁移

**Files:** `backend/app/{models/fx.py,schemas/fx.py,api/v1/fx.py,services/fx/trading.py,services/fx/borrow_buy.py}`；Alembic 新迁移；`backend/tests/test_fx_borrow_buy.py`、相关 PG/迁移用例。
**Interfaces:** `execute_borrow_buy(db,user_id,pair_id,amount,borrow_amount,min_out,idempotency_key)->FxBorrowBuyResponse`；`quote_borrow_buy(db,user_id,pair_id,amount,borrow_amount)->FxBorrowBuyQuote`。响应含 `trade_id,pair_id,input_amount,borrow_amount,cash_amount,output_amount,fee_amount,post_price,replay,created_at`。报价增加 `available_cash,affordable,estimated_debt,estimated_equity,estimated_risk_basis,margin_status,executable,blocked_reason,expires_at,leverage,daily_rate,r_initial,r_maintenance`。

- [x] 写真实数据库行为用例：成功本金/现金/钱包、滑点与风控回滚、旧债计息、用途冲突与重放；先运行确认缺失接口失败。
- [x] 在现货 in-session 核心加入可选融资参数，只融资分支增加持久本金与 purpose；一次交易后准入后再调用不 commit 的 increase_debt。保留原普通交易接口/响应。
- [x] 在 `borrow_buy.py` 实现专用事务 wrapper、纯读报价；共享现有发现、估值和风险公式，响应在 commit 前物化，提交后仅发布。
- [x] 加请求/响应 schema 与认证路由，个人历史暴露融资拆分，公开流保持原字段。
- [x] Alembic 增列和融资约束；有融资记录拒绝 downgrade，否则恢复旧 schema；迁移用例验证旧数据和约束。
- [x] 运行融资、FX、loan、credit、audit 相关已有测试，修复实际失败。

### Task 2: 前端真实做多区与安全重试

**Files:** `thccb-frontend/src/{types/fx.ts,api/fx.ts,composables/useFxBorrowBuyQuote.ts,pages/Fx.vue}`，融资请求工具及相关单测。
**Consumes:** Task 1 的固定契约和上述路径。
**Produces:** 现金默认、显式融资输入、成交后风险、融资回执/历史、持久未知请求恢复。

- [x] 写请求持久化行为用例，覆盖重挂载同编号、明确失败清除、未知失败保留及用户切换不交叉清除；先确认缺失功能失败。
- [x] 实现金额字符串类型、API 和防抖报价；原参数匹配校验、销毁/输入变化废弃旧报价。
- [x] 复用 pending FX 思路实现融资持久请求；未知结果统一阻止新增交易，保留原参数恢复入口。
- [x] 在现有做多区加入融资开关与本金输入，显示现金/借款拆分、日利率、授信配置及预计含息债务/风险；提交后回执与刷新分离。
- [x] 个人历史识别 borrow_buy；前端相关单测、type-check、定向只读 lint、build。

### Task 3: 相关验证、性能及交付文档

**Files:** 文档 `docs/api.md`、统一信贷运行手册及本次验证记录；临时对照脚本置 `/tmp`。

- [x] 隔离 PostgreSQL 验证同编号并发、不同编号额度竞争和锁序；融资核心补充真实 HTTP 契约覆盖。
- [x] 同机小范围比较融资成交和借款再买入的 SQL/总耗时/P95，现金成交并发融资负载下比较锁等待和吞吐；简单和含空头账户。
- [x] 运行后端编译/导入、相关回归及前端完整现有单测/类型/构建；验证够用后停止，不扩展无关全站审计。
- [x] 更新接口与运行文档，写实际结果/限制；独立整段代码审阅并修复实质问题。

## 执行记录

- 初始工作区基线 b296972；部署准备时同步发现 origin/main 已为 7d46d7c。最终只将融资补丁移入独立主线集成目录，保留主线纯 AMM、统一信贷唯一模式和行情运行时。
- 旧统一开关字段不恢复；迁移前驱为 unified_credit_only_20261010，融资成功提交后沿用 notify_market_data_committed。
- 复用准入估值返回值，避免第二遍完整估值；融资请求绑定发起账号，旧共享认证刷新不能覆盖新账号凭证。
- 用户要求停止后续 review，停止新增审阅；修复已指出的账户加载条件后交付。
- 最终命令和结果见 docs/fx-borrow-buy-validation-2026-10-10.md。
