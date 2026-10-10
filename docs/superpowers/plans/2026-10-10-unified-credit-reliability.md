# 统一信贷可靠性与查询优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for inline execution, or superpowers:subagent-driven-development only if the user selects delegation. Steps use checkbox (`- [ ]`) syntax for tracking. 用户已确认实施；后续讨论将请求编号和去重延期，本版取代原八任务回执表计划。尚未修改业务代码。

**Goal:** 修复本次六项检查中的提交结果、冻结提示、风险展示、重复估值、扫描统计和文档问题；请求重试去重另列待办。

**Architecture:** 保留资金、风险引擎和 writer 的现有职责与锁顺序，借还款操作响应与 GET quota 解耦。统一估值生成可复用空头快照，纯账户展示投影同时服务 user/loan 与风险组件。本轮不新增贷款回执表或请求编号，不改现有 FX 幂等性。

**Tech Stack:** 现有 FastAPI、SQLModel/SQLAlchemy async、PostgreSQL、SQLite、Vue 3、Pinia、TypeScript，不增加依赖。

**Spec:** [按用户最新范围修订的设计](../specs/2026-10-10-unified-credit-reliability-design.md)。[延期待办](../specs/2026-10-10-trade-request-idempotency-deferred.md)不是本轮实施依赖。

## Global Constraints

- 保持利率、精度、杠杆、费用、锁金与增险准入公式。保持门闩集发现、等待门闩时释放事务、品种行先于 User 行的锁顺序、锁内版本复检、强平 run/action 幂等性及一次扫描最多处理一组的规则。
- 保持现有技术栈，不增加依赖。没有资金变动时不新增请求记录，失败申请不留成功流水。
- UI 默认最多截图，不启动浏览器自动点击、填写表单或交互流程测试。
- 测试遵循 AGENTS.md；只为真实故障和关键行为补覆盖，不为文案或延期项新增测试。
- 保护其他既有未跟踪文档，不混入其他方案或生产数据操作。
- 主 agent 顺序实施；没有用户选择委派时不启动子 agent。发布和生产数据操作需要单独授权。

## Review Focus

- commit 成功后不能因 User refresh 或额度查询异常返回操作失败；commit/网络超时可能结果未知，不自动重发。
- 还款已无债或金额被封顶：返回真实 effective，不新增无意义流水或经济版本。
- 信用冻结与风险健康分别展示，冻结下还款和安全回补继续遵守原规则。
- 迟到 POST/GET 与账号切换：不串账户、不覆盖更新的状态，不用旧额度继续提交。
- 小额/非整数杠杆、未知 K、全停/reduce-only：等级、数学报价与减险可执行性保持各自语义。

## Task 1：借还款提交结果与估值解耦

**Files:** 修改 `backend/app/api/v1/loan.py`、`backend/app/schemas/loan.py`、`backend/tests/test_loan_api.py`、`backend/tests/test_credit_wp6a1_admission.py`。

**Interfaces:** 请求 body 不变，不添加请求编号；LoanActionResponse 保留 cash/debt/effective，max_borrow 可空；GET quota 的 max_borrow 仍非空。统一借款复用 decision.max_borrow，legacy 借款复用已读取持仓值，还款返回 max_borrow=null。

- [ ] 扩展现有 API/准入用例，对额度读取注入故障：借 10、还 5、全部还款均正常返回实际操作结果，数据库余额/债务与流水一致；既有额度检查和还款封顶断言保留。
- [ ] 在 commit 前构造并校验响应，删除 commit 后 User refresh/持仓/完整 quota 查询，保持原资金、利息、审计事务。
- [ ] 检查明确拒绝的操作不写资金流水，已无债还款保持无操作语义；不为未知网络结果新增请求记录或重放逻辑。
- [ ] 运行 `cd backend && venv/bin/python -m pytest -q tests/test_loan_api.py tests/test_credit_wp6a1_admission.py tests/test_ledger_loan_service.py tests/test_credit_pending_debt.py`；预期通过。
- [ ] 提交 `fix(loan): separate committed operations from quota reads`。

## Task 2：前端成功结果与一次刷新

**Files:** 修改 `thccb-frontend/src/api/loan.ts`、`stores/loan.ts`、`stores/__tests__/loan.spec.ts`、`pages/loan/Loan.vue`。

**Interfaces:** 保留 loanApi/store 的现有操作签名，无请求键；LoanActionResult.max_borrow 可空。通过现有操作结果展示成功，GET quota 负责当前账户快照。

- [ ] 扩展现有 loan store 用例：POST 成功但 GET 失败仍返回成功操作结果；quota=null，错误说明是账户刷新失败；一次成功操作只 GET 一次。保留刷新期间清除旧现金断言。
- [ ] 页面展示实际成功金额，随后刷新失败提示“操作已完成，账户信息刷新失败”；不将旧操作快照当成当前额度。
- [ ] 保留提交期间 busy 防双击，超时提示结果可能已执行，建议核对账户；不自动重新发送 POST，不新增 sessionStorage 请求状态机。补账号切换/乱序 GET 的真实 store 回归用例。
- [ ] 运行 `cd thccb-frontend && npm run test:unit -- src/stores/__tests__/loan.spec.ts && npm run type-check`；预期通过。
- [ ] 提交 `fix(loan-ui): distinguish operation and account refresh results`。

## Task 3：复用空头估值与冻结展示

**Files:** 修改 `backend/app/services/credit/valuation.py`、`backend/app/services/fx/shorts.py`、`backend/app/api/v1/user.py`、`loan.py`、`backend/app/schemas/user.py`、`loan.py`、`backend/tests/test_credit_presentation.py`、`test_credit_short_valuation.py`；新增 `backend/app/services/credit/account_read.py`；修改 `thccb-frontend/src/api/loan.ts`、`types/user.ts`、`pages/loan/Loan.vue`。

**Interfaces:**
- valuation 内新增 `ShortPositionValuation`，字段按 spec；`AccountValuation.short_positions: tuple[ShortPositionValuation, ...] = ()`。
- `classify_account_risk(thresholds: RiskThresholds, *, equity: Decimal | None, debt: Decimal, positive_assets: Decimal | None, short_cover: Decimal | None, blocked_reason: str | None) -> Literal['healthy', 'warning', 'danger', 'blocked']`。
- `account_risk_fields(valuation: AccountValuation, thresholds: RiskThresholds) -> dict` 从 API 辅助函数迁到 account_read，保持原字段行为。
- `short_position_fields(position: ShortPositionValuation, *, fx_enabled: bool, unified_enabled: bool) -> dict` 纯构造既有公开空头字段；`build_short_positions(valuation: AccountValuation, *, fx_enabled: bool, unified_enabled: bool) -> list[FxShortPositionPublic]` 无 SQL/报价。
- `borrow_blocked_reason(...)` 对 loan_enabled、两类冻结、估值未知和额度限制按 spec 优先级返回代码；GET quota 与统一 UserSummary 增加 credit_frozen/new_risk_frozen/borrow_blocked_reason。

- [ ] 扩展现有展示用例，验证健康账户用户冻结、运营冻结、借款关闭时额度与原因对应，但 risk_status 仍 healthy；冻结下仍可还款。冻结与估值错误同时存在时分别返回原因，不复用 blocked_reason 混淆。
- [ ] 将 currency_code/proceeds_basis 等加入现有批量短仓查询，为每条需要公开展示的记录生成同一 now 的欠币/quote 快照；保留零欠币但有锁金/基准、未知 K、全停及 reduce-only 的行为。
- [ ] 两个账户 API 用 valuation.short_positions 做纯投影，删除逐 pair 的 own_short_positions 路径与 loan→user 辅助函数依赖。account_read 中 valuation 类型只用 TYPE_CHECKING 导入，FX shorts 按需局部导入投影函数，防止 valuation→shorts→account_read→valuation 循环。单仓 FX 读取复用纯投影，并保持原 404、零仓、公开原因码与 fx_enabled 逻辑。
- [ ] 扩展现有短仓/展示用例断言公开明细与 K、pending debt 相符，GET 不改计息时点；交易关闭时保留数学 K，明细 executable=false。不得漏出借币库存/他人仓位。
- [ ] 前端映射具体借款限制文案，避免“风险健康但只能看到无额度”的提示；原始估值原因保留供诊断，常见玩家提示转为中文。
- [ ] 运行 `cd backend && venv/bin/python -m pytest -q tests/test_credit_presentation.py tests/test_credit_short_valuation.py tests/test_fx_short_api.py tests/test_credit_valuation.py`；单仓读取由现有 test_short_read_is_own_position_and_reference_cover_without_clock_advance 与 test_read_marks_fx_disabled_order_block_keeping_reference_cost 覆盖。
- [ ] 按 Task 7 定向记录 0/1/3 对查询数和输出一致性；提交 `perf(credit): reuse account short valuations and expose borrow gates`。

## Task 4：共享组件使用权威风险等级

**Files:** 修改 `thccb-frontend/src/components/user/CreditRiskStatus.vue`、`MarginStatusCard.vue`、`pages/loan/Loan.vue`、`pages/Fx.vue`、`api/fx.ts`；修改 `backend/app/schemas/fx.py`、`services/fx/shorts.py`、`backend/tests/test_fx_short_api.py`；新增 `thccb-frontend/src/utils/creditRiskStatus.ts`，扩展现有 `utils/__tests__/fxPresentation.spec.ts`。

**Interfaces:**
- `CreditRiskStatus.authoritativeStatus?: 'healthy' | 'warning' | 'danger' | 'blocked' | 'unknown' | null`。
- 生产组件调用 `resolveCreditRiskStatus(...)`，统一模式不重新比较展示 ratio；legacy 保持原门槛与 protected 行为。
- 内部 `FxShortQuoteRead` 与 HTTP `FxShortQuoteResponse` 均新增 margin_status，为 healthy/warning/danger/blocked，独立于原 risk_status=ok/blocked。用 Task 3 的 classify_account_risk 及 value_post_state 的 equity/debt_after/holdings_value/short_cover_cost 判定；所有估值短路/不可用构造分支填 blocked。

- [ ] 在现有前端展示测试调用真实 resolveCreditRiskStatus：ratio=0.25、initial=1/3.5，但 authoritativeStatus=healthy 时保持 healthy；blocked 优先于 noRisk，加载失败为 unknown，legacy 边界与保护态保持。
- [ ] 在现有后端报价用例补充同一 E/D/A/K 边界的 margin_status=healthy 断言；保留未知估值及低保证金可减险回补的有效断言，不复制开空准入公式。
- [ ] 组件接入生产 helper，贷款、账户卡、FX 当前账户和成交后预估四个入口分别传 quota.risk_status、summary.risk_status、quote.margin_status；加载失败不沿用旧等级。修整触及的账户提示，避免拼接“xxx · xxx”。
- [ ] 运行 `cd backend && venv/bin/python -m pytest -q tests/test_fx_short_api.py` 及 `cd thccb-frontend && npm run test:unit -- src/utils/__tests__/fxPresentation.spec.ts && npm run type-check`。如需要视觉核对只截图，不操作页面。
- [ ] 提交 `fix(credit-ui): display authoritative portfolio risk status`。

## Task 5：扫描统计与重试耗尽

**Files:** 修改 `backend/app/services/credit/sweep.py`、`execution.py`、`backend/tests/test_credit_sweep.py`、`test_credit_execution.py`，复用 Task 3 分类函数。

**Interfaces:**
- execute_user 返回原有状态或新增 `'retry_exhausted'`。
- 扫描新增 valuation_blocked_count/execution_blocked_count/retry_exhausted_count/valuation_duration_ms；blocked_count 为初筛和执行阻塞用户集合并集。soft_warning_count 为扫描快照真实 warning 人数。
- 现有 errors/deadlocks、scanned/triggered/recovered、execution_duration_ms/max_user_execution_ms 保留。

- [ ] 扩展真实扫描用例：L=20、m=.04、cash=105/debt=100 的用户为 warning，soft_warning_count=1，不执行资金动作。再用未知 K 用户验证 valuation_blocked_count，与执行 blocked 同用户时 blocked_count 不重复。
- [ ] 在现有执行用例制造有限版本冲突，断言返回 retry_exhausted、余额/持仓/审计未变，下一次条件恢复后可重新执行。扫描分类用现有夹具验证 retry_exhausted_count，不把 mock 的成功当资金验收。
- [ ] 实现扫描分类、用户集合统计与批量估值计时；结构化日志区别 version/economic drift、selection drift 和 ShortRetryCredit。保留 PAGE_SIZE=20/WORKERS=3，不修改重试次数和 GATES/事务顺序。
- [ ] 运行 `cd backend && venv/bin/python -m pytest -q tests/test_credit_sweep.py tests/test_credit_execution.py tests/test_fx_short_liquidation.py tests/test_credit_liquidate_group.py`，预期通过且原分页、防重入、恢复测试保留。
- [ ] 提交 `fix(credit): report scan warnings and exhausted retries`。

## Task 6：更新运行手册与接口说明

**Files:** 修改 `docs/unified-credit-risk-2026-10.md`、`docs/unified-credit-risk-validation-2026-09-30.md`、`docs/README.md`，必要时引用 `backend/scripts/change_loan_daily_rate.py`、`backend/app/services/credit/rate_maintenance.py` 的实际参数与护栏。

- [ ] 说明操作响应中的可空 max_borrow、新增冻结原因、权威风险等级、扫描字段和一次刷新方式；链接延期待办，明确贷款/LMSR 暂不提供请求去重，未知结果不自动重发。
- [ ] 将“旧利率结息未实现”改为“已有离线维护工具，在线仍禁止直接改息”；写清停写、PostgreSQL 事务所有权、同一 T 结息、审计和启动缓存。仅冻结新增风险不足以执行离线改息；不替用户运行该命令。
- [ ] 保留旧验证记录的历史事实，增加本轮修复/验证链接，不把新结果回写成 9 月已完成；未完成发布门槛继续明确保留。
- [ ] 检查文档链接，核对现有 CLI 的必填 `--rate` 和 `--operator-user-id` 参数及实际 `--help`，无需新增永久测试。提交 `docs(credit): document refresh behavior and offline rate maintenance`。

## Task 7：集中回归、PG 与定向性能证据

**Files:** 修改本计划任务勾选；新增 `docs/unified-credit-reliability-validation-2026-10-10.md` 记录实际证据。合成数据和测量脚本放 `/tmp`，不进入业务代码。

- [ ] 核对变更 diff 和实际测试覆盖：保留旧余额/审计/费用/锁顺序断言；新增测试只对应上述真实缺口。确认两个服务接口不循环导入，所有 CreditRiskStatus 调用都已接入正确等级。
- [ ] 定向查询测量：同配置、同一时点构造 0/1/3 个短仓账户，分别测 quota 和 summary 的 SQL 次数与结果；新次数不能随空头数线性增长，明细投影不再调用短仓报价。记录优化前后次数，不把常量条数写成永久 schema/实现断言。
- [ ] 定向耗时测量：同机合成组合，固定 100 个候选、3 FX+1 LMSR，预热一次、测三次扫描；每次使用相同种子的独立重建合成数据，不能连续扫描已被强平改变的组合来比较耗时。与改动前保持相同数据和开关；记录总耗时、错误、结果分类和真实资金动作。复用已有隔离测量工具，不新建大型压测层；若旧隔离基线不可复现，明确只报告当前结果，不编造百分比或生产 P95。
- [ ] 后端相关集中回归：`cd backend && DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-credit-final-tests.db venv/bin/python -m pytest -q tests/test_credit*.py tests/test_loan*.py tests/test_ledger_loan_service.py tests/test_fx_short*.py tests/test_user_summary_contract.py`；确认最终选中范围包含修改文件。pg 不混入 SQLite，不增加延期请求编号对应的迁移或测试。
- [ ] PG 在确认为可丢弃且名称含 test 的隔离库运行：`cd backend && TEST_PG_DATABASE_URL=<isolated-test-url> venv/bin/python -m pytest -q -m pg tests/pg/test_pg_credit_admission_contention.py tests/pg/test_pg_credit_liquidation.py tests/pg/test_credit_pg.py`；覆盖借款串行化、门闩等待释放连接、强平恢复与既有模型兼容性，不验证延期的请求去重。未能运行时记录未验证，不能宣称并发通过。
- [ ] 前端运行 `cd thccb-frontend && npm run test:unit && npm run type-check && npm run build`；触及文件 ESLint 只读检查，不使用全项目 `--fix`。如有依赖问题先核对项目已锁定依赖；不顺便升级工具链。
- [ ] 记录现有 pytest-timeout 配置警告，核对 requirements 中已声明的插件；环境缺插件时只修复验证环境，不扩大产品依赖。相关检查通过后结束验证，不重复全量或扩成上线压测。
- [ ] 记录所有实际命令、结果、warnings 和未验证项，说明没有部署、改息、生产数据库迁移或开关变更。完成 review 后提交实现与证据，报告用户已批准的集成方式；本计划不默认推送或发布。

## 覆盖映射

| 本次发现 | 实施任务 | 核心验收 |
| --- | --- | --- |
| 提交后错误返回 | 1–2 | 成功操作不依赖提交后的估值，刷新失败单独显示 |
| 冻结原因缺失 | 3 | healthy 与具体冻结原因分别正确返回/展示，允许还款 |
| 展示等级不一致 | 3–4 | 账户/贷款/FX/预估采用权威等级，legacy 保持 |
| 重复估值与逐 pair 查询 | 1–3、7 | POST 不读完整 quota，GET 一次，明细复用同次快照 |
| 扫描统计缺失 | 5 | 警戒、初筛/执行阻塞及重试耗尽可识别 |
| 文档过时 | 6 | 离线改息、响应契约和实际验证边界准确 |
| 重复请求可能再次执行 | 延期 | 见交易请求编号待办，本轮不承诺一次执行保障 |

后续实施按 1→7 顺序进行；用户当前讨论期间不擅自启动业务改动。请求编号/去重重新启动时再按待办评估方案与负载，不恢复已取消的独立回执表设计。
