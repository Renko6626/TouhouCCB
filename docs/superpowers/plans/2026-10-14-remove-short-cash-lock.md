# 删除空头资金锁定实施计划

> 按已确认路线执行；后台交易、前端本地预估、历史审计是明确边界的工作，使用 executing-plans 和 dispatching-parallel-agents 完成。

**Goal:** 空头卖出所得进入统一现金，不再逐仓锁定；沿用全账户保证金与真实借币 AMM。

**Architecture:** 删除实时模型、接口和资金路径中的 restricted_gold/restricted_cash、释放锁金逻辑；available_cash 保留为等于 cash 的现金余额，不表示保证金允许支出的额度。保留 proceeds_basis_gold，用于真实平仓收益计算。历史审计允许读取旧锁金字段，但新事件不写锁金，实时对账不再比较已删除列。

**Tech Stack:** 现有 SQLModel / SQLAlchemy / Alembic / FastAPI 与 Vue / TypeScript，无新增依赖。

**Spec:** 本次对话确认的真实借币、统一保证金、取消锁定方案。

## 约束

- 保留保证金公式、初始/维持门槛、计息、同币净方向、跨零最终检查、纯减仓入口、现有自动还款设置。
- 有债禁止兑换/付费弹幕继续保留；不增加自动筹资、自动卖资产或新的恢复机制。
- 不更改既有基线迁移；新增迁移删除锁金列与更新约束，保留现金、欠款、收益基准及历史记录。
- 已有成功回执可带历史多余字段，解析时忽略；不修改历史审计数据。
- 保留工作区原有改动，不提交无关文件；实现阶段不操作运行数据库。用户后续已授权提交推送并触发现有自动部署。
- 验证只运行相关已有测试、必要行为覆盖、前端单测/类型检查/构建；不运行浏览器交互。

## 实施与验收

- [x] 后端：模型/schema、报价与成交、贷款还款、估值与风控快照、强平预算、管理员操作不再扣锁金；新增数据库迁移。
- [x] 前端：本地模拟与保证金检查使用统一现金；删除锁定展示与确认文案，继续显示初始/维持门槛。
- [x] 审计：新开仓/回补/核销不记录锁金；旧事件继续可重放，新旧事件混合可衔接，对账保留本金/利息/基准/国库校验。
- [x] 测试：更新失效的锁定预期；复用真实交易用例覆盖空头所得还金债/购入其他资产、保证金不足拒绝、统一现金回补、资金不足回滚、迁移保留数据和约束。
- [x] 集中验证、检查差异与残留引用、最终审阅，记录命令和未验证项。

## 重点检查

统一交易和独立空头入口资金口径一致；前端与后端都保留成交后准入；强平现金不重复预留或错误优先还光金债；历史事件不因删除模型列失去重放能力；旧仓位迁移不能重复增加现金（原锁金本来就在 cash 中）。

## 执行记录

- 在当前用户工作区执行，保留原有文档/预览改动；无需重新批准已确认路线。
- 迁移回滚仅恢复列（零值）与旧约束，无法重建已取消的历史资金用途分配；不修改真实现金/负债。

## 验证结果

- 后端相关分批 pytest：299 个交易、欠币、现金、管理员、强平、报价、挂单及迁移用例通过。初轮出现的旧锁金预期和夹具问题已修正；交易账户读取字段索引遗漏由保留测试捕获并修复，受影响的 74 个用例最终重跑通过（16.61 秒）。
- 补充现有检查：`DATABASE_URL=sqlite+aiosqlite:////tmp/thccb_unlock_root_related.db backend/venv/bin/python -m pytest -q` 后接 credit_risk/config/valuation/thresholds/wp6a1_admission/wp6a2_lmsr/execution/liquidate_group/wp6b_fx_ops/wp6b_consumption/final_guards/startup_gating、loan_service/ledger_loan_service/loan_api/loan_admin、admin_amnesty、writer_buy/sell/e2e、danmuku_service、redemption_debt_guard 测试文件，共 227 passed（39.66 秒）。
- 审计：分别执行 `test_fx_short_audit_replay.py`、`test_fx_audit_replay.py`、`test_audit_events.py`，6 / 2 / 4 passed；混合新旧审计事件继续重放并与实时本金、利息、收益基准及国库对账。
- 前端在 thccb-frontend 执行 `npm run test:unit`：23 文件、254 passed；`npm run type-check`、`npm run build` 均 exit 0。改动 TS/Vue 文件只读 ESLint exit 0，未运行 --fix。构建仍有既有分块提示。
- `backend/venv/bin/python -m compileall -q backend/app backend/tests backend/alembic/versions` 与 `git diff --check` 均通过。
- 独立只读审阅未发现 Critical / Important 问题。未运行 PG 并发测试或浏览器交互；未做生产性能测量、运行数据库迁移或部署。

## 后续页面调整与交付

- 按用户要求精简贷款页常驻说明，空头卡数量/金额显示两位小数；保证金卡显示现金、持仓变现净值、本币债务、折算后的外币回补成本，并用清算净值 / 风险基数展示现有保证金率。门槛与触发线同一行，未改变风控公式。
- 1440px 桌面、390px 手机只读截图使用临时示例数据，未点击/填写表单或执行交易；无页面运行错误和横向溢出。截图保留在本地 output/unified-cash-pages，不纳入部署提交。
- 用户已明确要求提交推送、触发 main 现有 CI/CD；不修改工作流，不纳入原有其他文档及预览改动。

## 提交前最终检查

- backend 工作目录：`DATABASE_URL=sqlite+aiosqlite:////tmp/thccb_unlock_pre_push.db venv/bin/python -m pytest -q`：1472 passed、1 skipped、44 deselected，292.45 秒。保留的 SQLite 连接回收警告未导致失败。
- backend 工作目录：`DATABASE_URL=sqlite+aiosqlite:////tmp/thccb_unlock_pre_push_e2e.db venv/bin/python -m pytest -q --noconftest tests/test_fx_end_to_end.py`：14 passed，2.36 秒。
- thccb-frontend 工作目录：`npm run test:unit`：254 passed；`npm run build`：exit 0，19.05 秒。
- `git diff --cached --check` 通过。仅提交本任务代码、迁移、相关测试与三份当前功能文档及本计划；六份原有文档修改、预览源文件及截图未纳入。
