# 统一信贷可靠性与查询优化设计

状态：用户已确认本轮信贷优化；2026-10-10 后续讨论将贷款及买卖请求编号/去重延期。本轮实施、集中验证及最终补修复核已完成，记录见[本轮证据](../../unified-credit-reliability-validation-2026-10-10.md)。本版取代原独立回执表方案。

## 目标与范围

完成六项修复：资金提交后的错误返回、冻结原因缺失、风险等级展示不一致、重复额度查询和空头逐对查询、扫描统计缺失、改息文档过时。

成功标准：已提交资金不会因随后估值读取失败被错误报告为操作失败；冻结与保证金状态分别展示；统一模式所有共享风险组件采用后端等级；账户读取不随空头交易对数量增加数据库查询；扫描能识别警戒、估值阻塞和重试耗尽；文档准确说明离线结息改息。

保持 FastAPI、SQLModel/SQLAlchemy async、PostgreSQL、SQLite、Vue 3、Pinia、TypeScript，不增加依赖。保持利率、精度、杠杆、费用、锁金与增险准入公式。保持门闩集发现、等待门闩时释放事务、品种行先于 User 行的锁顺序、锁内版本复检、强平 run/action 幂等性及一次扫描最多处理一组的规则。

## 已有证据

- 隔离 SQLite 复现：额度查询在 commit 后抛异常，第一次借 10 后 cash=110/debt=10；重复同样调用后 cash=120/debt=20，两次调用均报错。还 5 后调用报错，但实际 cash=115/debt=15。
- 信用冻结账户返回 enabled=true、risk_status=healthy、max_borrow=0、blocked_reason=null。
- 合法杠杆 L=4.5、维持率 0.1，E=0.000001、D=0、A=0、K=0.000004 时，后端判 healthy，向上量化 B 后的展示比率为 0.25，小于初始率 1/3.5；组件会自行判 warning。
- POST 后端计算完整 quota，前端收到结果再次 GET quota。own_short_positions 查询 pair IDs 后逐 pair 调用 read_short_position，每对至少再查询 pair 与 position。
- sweep.soft_warning_count 没有累计；执行重试耗尽返回 skipped；扫描初筛未进入执行器的估值阻塞不能通过现有 blocked_count 完整识别。
- rate_maintenance.py 已有离线、旧利率结息与修改新利率同一事务的实现，手册仍写未实现。
- 2026-10-10 专项基线：后端 credit/loan 411 passed，前端 loan store 1 passed。后端 venv 未加载 pytest-timeout，有 timeout/timeout_method 配置警告。不是 PostgreSQL 并发或上线验收结论。

## 已确认范围与延期项

用户要求将交易请求编号及重试去重写进[后续待办](2026-10-10-trade-request-idempotency-deferred.md)。本轮不新增回执表，不扩展 LedgerEntry/Transaction 的请求身份字段，不新增请求键要求、幂等重放接口、零操作流水或 sessionStorage 未确认订单状态。现有 FX 幂等性保持。

用户接受当前未保护请求超时后再次执行的可能性；额度检查不能保证重复借款/部分还款失败。此延期不放宽资金原子性、现有风控、锁序或强平幂等性。重复交易和账务失衡分别判断，不宣称延期后不会发生任何资金故障。

## 借还款提交边界

三个玩家写接口的请求 body 和认证方式保持。LoanActionResponse 保留 cash/debt/effective；max_borrow 允许 null，借款能复用已完成的准入判定时填写，还款不为填充该字段读取完整组合。GET quota 的 max_borrow 仍为非空 Decimal。Decimal 保持现有序列化，不新增回执身份字段。

资金变动、计息审计和现有流水仍在同一事务提交。响应在 commit 前构造并校验，commit 后不再 refresh User、不查持仓或 quota。统一借款复用 decision.max_borrow；旧模式借款复用准入时已读取的 holdings value。明确失败的操作回滚，不为失败申请新增记录；已无债的还款保持当前无操作语义。

commit 本身或网络超时仍可能使客户端不知道实际结果；本轮不提供原请求结果重放，不自动重发未知结果的借还款。原型中的“每次成功只执行一次”保障已移入延期待办。

## 前端操作与刷新

收到成功操作结果后展示实际借入/还款结果，再 GET quota 一次。quota 失败显示“操作已完成，账户信息刷新失败”；旧 quota 仍置空，禁止使用旧可用现金或旧额度继续提交。保留当前 submitting/busy 防双击，不新增订单状态机或请求编号。

超时或断网提示结果可能已执行，建议先刷新账户后核对；刷新账户本身不声称能证明某一请求是否执行。用户再次手动提交按新请求处理。防止迟到 GET 或旧用户响应覆盖当前账户状态。

## 账户估值与展示边界

valuation.py 仍负责现有批量读取与估值。增加内部 ShortPositionValuation 快照，保存已读取的币种代码、本金、利息、时点、锁金、所得基准、含息欠币及 FxShortQuote/未知原因；AccountValuation 增加 short_positions，默认空 tuple，兼容既有构造。现有短仓批量 JOIN 增加所需列，不新增逐 pair 查询。统一估值时点 now 同时用于含息债务和明细；只做数学报价，不读取借币库存/其他用户头寸。

新增 credit/account_read.py，仅负责纯展示投影：风险等级、公开空头 DTO 和资金用途展示，不 commit、不抓锁、不重新报价。user.py、loan.py 不再相互导入账户辅助函数。AccountValuation 的类型依赖使用 TYPE_CHECKING，FX shorts 按需局部导入投影函数，避免现有 valuation 对 pending_short_debt 的依赖形成循环。FX 单仓读取复用相同的纯空头 DTO 构造器，独立接口仍只查目标仓位，不为读一个仓位计算整个组合。

空头 DTO 的可执行性继续区分数学报价是否完整、pair 全停/reduce-only、fx_enabled 和统一信贷开关；没有正钱包但仍有空头欠币的记录仍返回，完全清仓的零记录保持现有单仓/列表语义。原 ck_fx_short_position_state 禁止零欠币而锁金/所得基准非零，不改该约束。保留原公开错误码及 None 语义。组合 E/B 不以展示明细的 executable 反推，关停交易不伪造 K=0。

LoanQuotaResponse 与统一模式 UserSummary 新增 credit_frozen、new_risk_frozen、borrow_blocked_reason。借款原因优先为 loan_disabled、frozen_by_operator、credit_frozen、valuation_unavailable、insufficient_initial_margin、no_borrow_headroom；无阻塞为 null。blocked_reason 继续表示估值原因。healthy 与冻结可以同时存在，页面分别说明；冻结不阻断还款、安全减仓和回补。

## 风险等级展示

共享 classify_account_risk(thresholds, *, equity, debt, positive_assets, short_cover, blocked_reason) -> healthy/warning/danger/blocked，使用已有 RiskThresholds 精确比较函数，不用向上量化后的 B 或百分比重新判级。

CreditRiskStatus 增加 authoritativeStatus prop：统一模式有后端数据时直接用它；数据加载失败时传 unknown；legacy 才沿用本地门槛比较。noRisk 只改变无风险占用展示，不覆盖后端 blocked；健康、警戒和危险以权威等级为准。

贷款页、MarginStatusCard、FX 当前账户面板全部接入。FX 交易后预估目前 risk_status=ok/blocked 仅表示估值可用性，因此在内部 FxShortQuoteRead 与 HTTP FxShortQuoteResponse 同时新增 margin_status，使用 value_post_state 的 E、D、A、K 精确判级；保留原 risk_status、executable、risk_blocked_reason，回补可执行性不被 margin_status 反向禁止。估值短路/不可用时 margin_status=blocked。

## 扫描与观测

soft_warning_count 统计本轮已扫描且完整估值判 warning 的用户。valuation_blocked_count 统计初筛估值阻塞；execution_blocked_count 统计执行器阻塞；blocked_count 为两类用户集合的并集人数，避免同用户重复累计。既有 triggered/recovered/errors/deadlocks 字段继续保留。

execute_user 重试耗尽返回 retry_exhausted，sweep 新增 retry_exhausted_count，不再把它混入普通 skipped_count。仍保留下一轮扫描重试，不新增无限重试或行情触发强平。记录用户、执行结果、阻塞/重试原因；记录批量估值耗时及现有端到端执行耗时，不将其标成行锁持有时间。

本轮保留 PAGE_SIZE=20、WORKERS=3、分页上界、run-now 防重叠和锁内完整重新检查。优化查询不复用锁外缓存授权实际强平。批量估值异常仍报告本轮失败；不在本轮新增复杂的坏数据逐用户回退框架。

## 文档与验证

更新统一信贷手册、验证记录、docs 索引：在线修改仍受限制；离线改息需要停写并取得 PostgreSQL 事务级所有权锁，按同一 T 结清金债与外币债再修改利率；不得把只冻结新增风险解释为离线维护安全条件。引用现有 change_loan_daily_rate.py 实际参数，不执行生产改息。

测试遵循 AGENTS.md：优先扩展现有 API、展示、扫描、PG 和 loan store 用例，不为延期项新增测试。验证提交后不读取估值、明确失败回滚、实际还款封顶、账号与迟到响应隔离、GET 刷新失败和未知 K；组件只证明等级展示，不重复金融公式。

查询优化用 SQLAlchemy 查询记录作定向测量，并验证真实 DTO/余额：0/1/3 个空头时查询次数不随 pair 数增加，明细不再次调用报价。测量代码留在 /tmp；不新增源码扫描断言或大规模压测框架。PG 仅用可丢弃 test 库验证真实同键并发与原有锁顺序；UI 默认最多截图，不自动点击或填写。

最终运行相关完整回归和前端类型/构建检查，报告实际命令、失败、警告与未验证项。性能对照仅声称已测场景；不把功能回归或一次扫描耗时当成生产 SLO 验收。发布、生产迁移与开关变更需要单独授权。
