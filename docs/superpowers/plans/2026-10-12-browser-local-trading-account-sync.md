# 浏览器本地交易预估与账户同步 Implementation Plan

> **For agentic workers:** 使用 `superpowers:executing-plans` 按任务实施；只有用户明确选择委派时才使用 `superpowers:subagent-driven-development`。步骤用复选框记录真实完成情况。交接必须携带全局约束、接口、文件职责及验收证据，遇到新增架构决策返回主 agent。

**Goal:** 将 FX 输入过程的整笔报价和跨产品账户风险预估移到浏览器，行情只驱动本地重算，真实账户变化通过私人提示触发一次聚合刷新。

**Architecture:** 专用交易账户聚合 API 提供版本一致的基准；一个已认证 fetch SSE 承载所需组的完整定价状态和本人账户事件。前端纯函数负责模拟与估值，共享会话协调刷新、版本与旧请求恢复；后端真实成交／强平与资金事务保持现有权威。

**Tech Stack:** 现有 FastAPI、SQLAlchemy async／SQLModel、PostgreSQL／SQLite、Decimal；Vue 3、TypeScript、Pinia、Vitest、浏览器 fetch／ReadableStream、现有 BigInt 金额工具。当前不新增框架、数据库业务表或消息总线；数值依赖受 Task 0 门槛约束。

**Spec:** [浏览器本地交易预估与账户提示／同步设计](../specs/2026-10-12-browser-local-trading-account-sync-design.md)。基线 `main@559552f`。状态：2026-10-12 用户授权按 subagent 流程开始执行；实现 `gpt-6.1-sol / low`，审阅 `gpt-6.1-sol / medium`。先完成 Task 0 前置核对，超出既定提案的新架构选择仍按用户约定确认。

## Global Constraints

- 聚合 API 的范围与共用刷新已确认，不重复询问；spec §11 的具体传输、定价版本／字段、数值与恢复节奏通过 Task 0 固定后才改产品代码。
- 实际资金、授信、锁金、计息、强平与 AMM／LMSR 仍由现有服务端事务处理；不改变 OWNERSHIP、GATES、行锁序、经济版本和价格保护。
- 账户与定价是两个版本域；broker seq／last_ring_trade_id 不是完整定价版本。账户同步完成须满足 context 的每组最低覆盖下限。
- 金额与数学输入使用十进制字符串；账面净值和清算净值分别派生，风险用未量化 E/W。LMSR 组内滚动清算，不逐仓独立求和。
- 一个认证交易 SSE 可承载多个品种；私人事件仅本人可读，长期 bearer 不进 URL。公共图表接口、历史编码和公开字段白名单保持兼容。
- commit 后轻量通知，回滚／失败／成功回放不新增通知。推送故障不得改变已提交结果；末条通知／行情遗漏须由轻量校对恢复。
- 行情不触发报价／账户刷新；大刷新不带图表、称号或成交历史。至多一个账户刷新在途，连续事件合并，不能覆盖较新行情。
- 保留旧 pending 的账号、入口、完整参数与编号；通知不是回执，不解锁未知结果；纯平仓意图及显式融资不能随行情自动扩大。
- 沿用既有组件与页面，只调整本任务状态来源；不引入限价单、内存权威 FX writer、事件强平、永久通知中心或全站重构。
- 用户现存四份历史文档改动及截图产物不得混入本任务；实施前按 worktree 技能创建／确认隔离分支，文档转移不覆盖他人修改。
- 永久测试只补真实故障／关键行为，复用既有夹具，不做源码字符串／样式断言或多层复制。UI 默认最多截图，不自动点击、填表或真实交易。
- 本计划不授权 commit／push／合并／部署／业务数据库操作。产品实现、重要路线确认和后续发布遵循用户约定；已有检查通过后不重复扩大验证。

## Review Focus

| 真实风险 | 必须观察到的行为 | 所属任务 |
| --- | --- | --- |
| 新账户配旧池、旧查询覆盖新帧、服务端重启 | 达到每组覆盖下限才 ready；新 epoch 先重建 | 1、4 |
| 最后一次 commit／行情没通知，没有后续 seq gap | 15 秒轻量校对发现账户或定价落后并恢复 | 2、4 |
| 多空跨零、含息回补、同市场多 outcome、浮点门槛 | 金额／资金用途正确，整组估值，未知／误差不能显示健康 | 0、3 |
| 重复事件、强平分段、在途更高版本、其他标签页操作 | 通知准确且去重，合并刷新最终达标，无重复交易 | 2、4 |
| 切账号、凭证到期、已有未知成交、纯减仓 | 私有数据隔离，原请求身份保留，业务准入不被改写 | 2、4、5 |

## 文件职责与接口

| 文件／模块 | 唯一职责 |
| --- | --- |
| 新 `backend/app/schemas/trading_context.py` | 基准、定价组、覆盖令牌、事件与轻量校对响应的白名单 schema |
| 新 `backend/app/services/credit/trading_context.py` | 批量账户基准读取、前后版本复检、依赖集合与覆盖下限 |
| 新 `backend/app/services/trading_prices.py` | 只读已提交定价投影、组身份／revision、规则快照、共享发布；不拥有资金状态 |
| 新 `backend/app/services/account_events.py` | 仅本人经济变更消息的有界暂存／订阅、commit 后轻量 enqueue |
| 新 `backend/app/api/v1/user_trading.py`；修改 `backend/app/main.py` | 认证 context／版本／补价 API、单独 stream_router 和资源启停 |
| 既有事务 owner／行情 producer | 记录完成的经济变更，commit 后通知；定价变化推进共享读状态 |
| 新 `thccb-frontend/src/types/tradingContext.ts`、`api/tradingContext.ts`、`api/tradingStream.ts` | 类型、认证 HTTP、fetch SSE 解码／重连；不计算估值 |
| 新 `utils/fxSimulation.ts`、`utils/tradingRisk.ts` | 纯 AMM／净额模拟、计息、整组价值与双净值／风险派生 |
| 新 `stores/tradingSession.ts` | 基准／行情合并、覆盖／ready 状态、单刷新与计时协调；页面共用 |
| 修改 FX／LMSR 页面、user store／风险组件、notification store | 消费统一结果、原请求恢复和提示；保留旧调用者兼容 |

以下名称用于任务间交接，Task 0 后同步固定到 spec；不得由各任务猜另一套字段：

- `GroupKey = { product: 'fx' | 'lmsr'; id: number }`；字符串键仍为 `fx:<id>`／`lmsr:<id>`。
- `GroupStamp = { epoch: string; revision: string; input_hash: string }`；revision 为同 epoch 可比较的十进制整数串，hash 仅证明输入身份，不用于大小比较。
- `PricingGroup = GroupKey + stamp + as_of + 完整组输入`；FX 含 G/F、两侧费率及状态，LMSR 含排序 outcome_ids/q_dec、b、状态／截止时间。参考价由这些输入派生。
- `TradingContext = { schema_version: 1; account; rules; groups; minimum_group_stamps; as_of; server_time }`；account 含 economic_version、现金、持久金债、计息时点、仓位／成本／欠币／锁金及冻结；rules 含有效规则版本和实际生效参数。
- `AccountChanged = { event_id; economic_version; reason; occurred_at; operation_ref? }`；版本与 ID 不丢精度；operation_ref 只用于已知本人操作关联，不是成功回执。
- 新契约中的 revision／economic_version 统一按十进制整数串处理；与旧成交请求的整数 expected_economic_version 互转时严格检查安全整数范围，不能经 Number 静默截断。具体 schema 字段由 Task 0 同步固定。
- `TradingVersionCheck = { economic_version; rule_version; group_stamps; server_time }`；不做账户估值，校对缺价只补所需组。
- 会话状态：`syncing | ready | stale | error`；本地预估条件：`satisfied | rejected | unknown`。库存等提交时条件另列，不伪装成全条件 executable。

## Task 0：固定具体路线和实施基线

**Files:** 本计划、关联 spec；调查 `market_writer.py`、`writer_ops.py`、FX `amm/net_trading/trading/shorts`、credit `version/valuation/thresholds` 与前端 `api/fx.ts`。本任务不修改产品代码。

**交付：** 用户可审阅的具体契约、版本方案和数值误差合同；通过前不进入 Task 1。

- [x] 记录实际 HEAD、status、实施分支／隔离工作区与需要转移的本任务文档，原有用户改动保持原样。若主线已变化，只定向复核受影响接口。
- [x] 确认推荐认证传输为带 Authorization 的原生 fetch SSE，复用已有 token refresh／账号 generation；不新增票据表、URL token 或依赖。context／stream 接受最多一个 `current_group`，其余订阅组由服务端从本人账户推导。
- [x] 固定 API：`GET /api/v1/user/trading-context`、`GET /api/v1/user/trading-versions`、`GET /api/v1/user/trading-prices`（只补价）；推荐 SSE 为 `GET /api/v1/stream/trading`，在新模块用单独 stream_router 注册，复用已有 `/api/v1/stream/` 关闭缓冲／长超时的代理 location，优于 spec 中示意的 `/user/trading-stream`。四者都认证及白名单，不改变旧公共流权限。若实际部署代理不覆盖该路径，提出配置差异再确认，不擅自修改 deploy 红线。versions/prices 请求组必须属于本人账户或当前交易品种，不接受任意私人 user_id。
- [x] 固定无 schema 迁移的组版本提案：进程 epoch＋共享定价 registry 的单调 revision＋完整输入 hash；FX 输入同时保留 pool_version。registry 的组读取短锁内使用新短会话读取同一条 SQL 快照、物化输入后比较身份／安装 revision；输入刷新串行化，拒绝旧异步读取覆盖。LMSR 不复用 broker seq／ring 游标；重启全部失效，context 和轻量校对使用同一 registry。必要时验证只读实例每次基准／校对从其权威读源刷新，不声称跨进程 revision 可比较。
- [x] 固定初始化方案：先取得 context 并暂存，不能提前宣布 ready；随后建立 stream，服务端先订阅本人／组主题、捕获 anchor，再发送最新经济版本、规则和定价首包。首包达到 context 覆盖下限且经济版本一致后才整体安装，context 到订阅间的新账户变化触发合并重读。缓存不可用时读库恢复或返回可重试错误，不能只依赖过去的 enqueue。
- [x] 本地按现有 6 位金额／仓位精度处理已获用户确认，无需再提更高业务精度方案。在 `/tmp` 用生产 Decimal 和既有前端数值工具做小范围对照，覆盖存储边界、已有8dp费率、6dp输出／手续费、exact-output两步向上取整、含息欠币和LMSR极端q。FX优先BigInt定点／有理数，比例和中间运算不提前截断；利息／LMSR复用已有工具，不默认装库。若出现足以改变六位结果／风险提示的偏差，记录原因，局部修正舍入或标明unknown；确需新依赖才提交具体选择，不借此提高已确认的业务精度，不放宽预期掩盖失败。
- [x] 用户确认 200ms 事件合并、15s 活跃页面轻量校对、1s 本地计息；作为代码常量与文档注释，不新增后台／SiteConfig 配置项。全组合依赖覆盖与数值、认证流、完整 q 字段／版本读模块在前置核对中固定，出现超出既定提案的新决策先报告。
- [x] 将已选择方案及误差合同写回 spec／计划，列出经济版本覆盖清单和定价写入口清单；不把私有库存加入定价流。技术方案未确认时仅完成调查和文档准备。

**前置完成记录（2026-10-12）：** Task0报告与medium审阅均通过；无新增依赖或schema路线。数值要求收窄到现有6dp预估／生产舍入，不建设通用区间算术。基线前端201 passed、相关后端53 passed；实际证据见验证记录与计划工作区报告。

## Task 1：聚合基准、完整定价与版本校对

**Files:** 新 schema、`credit/trading_context.py`、`trading_prices.py`、`api/v1/user_trading.py`；修改 `main.py` 路由注册；新 `backend/tests/test_trading_context.py`，PG 竞态集中在新 `tests/pg/test_pg_trading_context.py`。

**Interfaces:** `read_trading_context(session, user_id, current_group) -> TradingContext`；`read_trading_versions(session, user_id, current_group) -> TradingVersionCheck`；`read_trading_prices(session, user_id, keys, current_group) -> list[PricingGroup]`。定价 registry 提供 `refresh_groups(keys) -> list[PricingGroup]`，所有消费者复用其 stamp。

- [x] schema 明确字符串数值、排序身份和可空未知字段；账户各表、规则和定价直接批量读取，不内部调用 HTTP，不运行 summary 的称号／排行榜／重复全估值。
- [x] 在真实持仓读取用例覆盖：本人含金债、两个 FX 组、一个 LMSR 组，接口只返回本人数据且含完整锁金／计息基准；缺 pair／outcome 明确 unknown，不静默漏组。既有 user summary 契约保持通过。
- [x] 实现独立于 SQLAlchemy identity map 的前后权威 economic_version 复检，最多 3 次完整尝试，每次换清洁读事务；失败返回 409 `account_context_changed`。定价读取在前账户基准之后，最后复检账号版本，保证每组 stamp 下限；不长持 User 行锁、资金 GATE 或连接等待 SSE。
- [x] registry 组内一次快照读取所有数学／状态字段；同输入身份保留 revision，变化推进。context、补价、首包和校对统一来源，FX 参考价不用 publication 的旧 post_price；规则 snapshot 取实际生效 flags／利率，不能提前应用“保存但重启后才生效”的设置。
- [x] 用可丢弃 PG 覆盖现金与仓位原子提交穿过读取窗口、新账户至少覆盖其池变化、串行组刷新与旧响应交错；断言实际基准一致或明确重试，不固定某个实现 SQL 顺序。没有接触业务库。
- [x] 运行 backend：`DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-trading-context-task1.db venv/bin/python -m pytest -q tests/test_trading_context.py tests/test_user_summary_contract.py tests/test_credit_valuation.py tests/test_credit_lmsr_quote.py`；PG 单独 `TEST_PG_DATABASE_URL=<独立临时库> DATABASE_URL=<独立SQLite> venv/bin/python -m pytest -q -m pg tests/pg/test_pg_trading_context.py`。记录结果与未验证项。

**完成条件：** 一个聚合请求得到可安装的完整基准；轻量校对不估值、不发交易写入；旧接口未变。

## Task 2：commit 后私人事件与单连接同步

**依赖：** Task 1。**Files:** 新 `account_events.py`；修改 `trading_prices.py`、`user_trading.py`、`main.py`。接入既有事务 owner：`writer_ops.py`／`market_writer.py`、FX `net_trading.py/trading.py/shorts.py/borrow_buy.py`、`api/v1/loan.py`、`credit/execution.py`、`admin_user_service.py`、`services/redemption.py/danmuku.py` 及其 API、`loan_sweep.py`；定价接入 `api/v1/admin_fx.py`、`tick_broadcaster.py`、`services/site_config.py` 和现有市场参数变更入口，实际清单来自 Task 0。兑换／消费服务目前通过 managed_transaction 提交，API 里的额外 commit 不能被误认作唯一资金事务。

**Interfaces:** `enqueue_account_change(user_id, economic_version, reason, operation_ref=None) -> None`、`notify_pricing_changed(keys) -> None` 均同步轻提示、不抛到资金响应；`trading-stream` 发 `sync`／`pricing`／`account_changed`／`rules`／心跳。有界私人队列独立于公开市场 broker 权限。

- [x] 复用既有账户 mutation／提交用例，针对实际成功、回滚、幂等回放各断言通知是否出现及内容；强平有多次 commit 时仅描述已完成减仓。new `backend/tests/test_account_events.py` 只覆盖队列／独立投递语义，不重复所有资金场景。
- [x] 所有实际改变交易基准的写入口维护 economic_version；调查计息／兑换／调账等旧入口缺口并局部补齐。底层服务可收集变更描述，真实 commit owner 才 enqueue，不能在 bump helper 或尚未 commit 的执行器广播。
- [ ] 覆盖每类 owner 至少一个真实提交行为，复用已有 `test_fx_net_trading`、`test_loan_api`、`test_admin_amnesty`、`test_credit_liquidate_group`、`test_writer_admin_ops`；兑换／消费用其已有夹具。事件查到更高版本不意味着原订单已成功，回执语义独立。
- [x] 实现单连接认证、依赖推导、首包补齐、账号身份隔离、连接退出退订；失效／过期凭证关闭连接并要求重新认证。认证／首包短会话退出后再进入 generator，不能为长连接持有数据库事务／连接。25s 以内心跳、服务端最多1h或凭证到期时断开；凭证校验不改红线认证核心文件。私人流每连接有界，慢消费者退出恢复，不无限缓存。
- [x] 定价 commit 通知按组合并，producer 共享读状态／消息；状态、费率、注撤资、截止和 rules 变化也覆盖。新 SSE 不携带 K 线尾段／成交聚合，旧图表继续用原流；同帧价格由整组输入生成，不每个订阅者重跑数学或查询完整账户。
- [x] 启动／关闭资源跟随现有单进程生命周期；非 owner／read-only 不产生经济写入，读源更新由首包／轻量校对恢复。队列溢出只影响推送，version 校对可发现最后一次未通知的变化。
- [ ] new `backend/tests/test_trading_stream.py` 在真实认证 HTTP 层覆盖仅本人事件、五组一个流、过期凭证、queue kick／清理和 context→subscribe 窗口漏通知；定价同帧用发布排队期间第二笔成交／参数变化证明。HTTP 层不重复资金守恒全矩阵。
- [x] 运行相关保留检查：`DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-trading-stream-task2.db venv/bin/python -m pytest -q tests/test_account_events.py tests/test_trading_stream.py tests/test_fx_net_trading.py tests/test_loan_api.py tests/test_admin_amnesty.py tests/test_credit_liquidate_group.py tests/test_writer_admin_ops.py tests/test_loan_sweep.py tests/test_redemption_service.py tests/test_danmuku_service.py tests/test_site_config.py`；其他涉及的管理员／规则测试按实际清单追加一次。

**完成条件：** 实际账户变更能发本人提示，五组定价可同流续写；通知失败不改变任何真实提交结果。

## Task 3：本地纯模拟与组合风险

**依赖：** Task 0 数值合同、Task 1 输入类型。**Files:** 新 `types/tradingContext.ts`、`utils/fxSimulation.ts`、`utils/tradingRisk.ts`；按需从 `api/fx.ts` 提取已有 BigInt 基础到 `utils/fxDecimal.ts`，旧 export 保持兼容；复用 `utils/lmsr.ts`。新 `utils/__tests__/fxSimulation.spec.ts`、`tradingRisk.spec.ts`，复用 `lmsr.spec.ts`；小型 `tradingPreview.golden.json` 仅含匿名真实后端输出和输入。

**Interfaces:** `simulateFxOrder(account, pricing, rules, order, now) -> FxOrderPreview`；`valueTradingAccount(account, groups, rules, now) -> AccountPreview`；`valueFxPostTrade(base, orderPreview, groups, rules, now) -> AccountPreview`。预估返回 legs、资金／仓位后态、双净值、E/W、risk status、已知拒绝／未检查原因和误差状态；不依赖 HTTP、Pinia 或 DOM。

- [x] 将 Task 0 已确认的精度／舍入／范围合同落实成少量行为用例；正好完整回补、第一腿费改变第二腿、6dp dust、8dp费率和大额 exact-output 不得被提前舍入破坏。
- [x] 实现 buy／sell／buyExactOut 与 net 顺序模拟；金债、全部锁金、本仓释放、利息先还、新融资仅给新增多头严格对照生产语义。同 pair 双方向异常拒绝本地新增预估，不迁移／免债。
- [x] 实现按 market/outcome 排序滚动 LMSR 组清算与 FX 多头卖出／欠币完整回补；新行情只计算改变组，利息时钟更新欠币组。交易后态使用交易后的储备重估当前组，不拿交易前价值加减成交额冒充净值。
- [x] 实现双净值、available cash、当前／交易后门槛、冻结／纯减仓规则；无债不比较增险保证金。缺少定价、不可完整回补或无法约束的误差返回 unknown；display 与 liquidation 的已知性按 spec 分别处理。
- [x] 在 `/tmp` 调真实 Python `amm/net_trading._simulate/credit` 生产逻辑取得匿名固定预期，记录来源 SHA／时点；独立手算少量明确守恒结果，不能在测试复制 TS 算法生成 expected。黄金向量不是自动更新快照，修改前解释行为变化。
- [x] 用跨零两个方向、非零费率、含息纯回补、不同仓锁金、LMSR 同市场两持仓、暂停／未知组、初始与维持门槛边界验证。多数场景只在纯函数层覆盖；已有 FX 持久资金测试保留，不在 HTTP 再复制。
- [x] 在 frontend 运行 `npm run test:unit -- src/utils/__tests__/fxSimulation.spec.ts src/utils/__tests__/tradingRisk.spec.ts src/utils/__tests__/lmsr.spec.ts src/utils/__tests__/fx.spec.ts` 和 `npm run type-check`。若数值合同实际不成立，停在本任务重新提交具体取舍，不能增大容差掩盖不一致。

**完成条件：** 固定输入下本地整笔结果符合生产契约；两种净值／未知风险明确，无网络调用。

## Task 4：认证 SSE 客户端、版本合并与刷新协调器

**依赖：** Task 1–3。**Files:** 新 `api/tradingContext.ts`、`api/tradingStream.ts`、`stores/tradingSession.ts`；按需修改 `api/index.ts`／`stores/auth.ts` 接现有 token 与账号generation、`stores/notification.ts` 去重；新 `api/__tests__/tradingStream.spec.ts`、`stores/__tests__/tradingSession.spec.ts`，复用 `userBoundRequest.spec.ts`。

**Interfaces:** `tradingContextApi.getContext(currentGroup)`／`getVersions(currentGroup)`／`getPrices(keys,currentGroup)`；`TradingStream.connect(currentGroup, handlers)`／`close()`；store `start(currentGroup)`／`stop()`／`requestRefresh(targetVersion?, reason?, operationRef?)`／`checkVersions()`，提供基准、组Map、ready状态、双净值／风险和纯本地preview。

- [x] fetch SSE 解析覆盖多行 data、分块 UTF-8、命名事件、心跳、断开／Abort与旧generation；401按现有refresh单飞机制重认证，不把未确认写请求自动重发，也不在每个行情包查token。
- [x] context 后建立流，首包sync校对context版本，初始化期间排队并按stamp合并；同epoch较旧revision丢弃、epoch不同清空覆盖、hash只作输入身份。到达全部minimum stamps前ready为false。
- [x] `requestRefresh` 立即失效旧准入，200ms 合并、一个在途、保留最大目标版本。响应覆盖目标且所有组达到下限才整体安装；新版本在途到达时补一次刷新。失败保留最后数据显示stale并有界退避，不每次SSE重试。
- [x] 本人响应与事件同一operation_ref共享刷新；通知按event_id去重，不因账户已同步丢掉真实强平／管理员文案。批量强平提示合并，计息静默；事件没有足够关联信息时允许一次独立提示但不推测原订单成功。
- [x] 行情帧只更新组及派生预估；1s统一时钟以服务器基准+单调经过时间计算，休眠／回前台重新校时。15s仅active版本核对；未变不拉context，组落后只getPrices，账户变才getContext；规则变化先重估／必要时刷新规则，不重新拉图表。
- [x] 账号切换废弃HTTP/stream/finally与通知；start/stop管理ref或路由owner，页面切换不留下重复计时器／SSE。连续切当前品种合并到最终generation。
- [x] store行为用例：五组包零quote/context调用、200ms事件合并、刷新期间更高目标、较新行情不回退、最后事件缺失被15s版本检查恢复、只有行情缺失时仅补价、后台停止校对、账号切换与通知去重。只对“无不必要网络请求”这一实际行为断言请求次数，不固定内部算法调用次序。
- [x] 运行 `npm run test:unit -- src/api/__tests__/tradingStream.spec.ts src/api/__tests__/userBoundRequest.spec.ts src/stores/__tests__/tradingSession.spec.ts`、`npm run type-check`，对改动文件只读 ESLint；不运行默认带 --fix 的全仓lint来顺手改无关文件。

**完成条件：** 网络调用跟随真实账户／覆盖变化，价变本地重算；无重复刷新、账号泄漏或无界任务。

## Task 5：FX／LMSR 页面接入与旧请求兼容

**依赖：** Task 4。**Files:** 修改 `pages/Fx.vue`、`pages/market/TradingView.vue`、`stores/user.ts`、`components/market/MarginCallBanner.vue`／`TradePanel.vue`／`FxBorrowBuyForm.vue`／`FxExecutionPrice.vue`；私人历史继续旧路径。按需修改 `api/fxNetTrading.ts`、`api/fx.ts`、`api/fxBorrowBuy.ts` 保持旧pending恢复；复用已有请求／展示／user store测试。

**Interfaces:** 页面只读共享TradingContext／AccountPreview；FX新订单用本地 FxOrderPreview生成原net请求。LMSR保留buyCost／sellProceeds和原writer接口，账户ready／risk改接共享会话，旧user store调用者不被全站迁移。

- [x] FX常规输入移除useFxNetQuote必经请求，用本地preview；首次和账户恢复用聚合context替代交易基准相关的summary/wallet/short多请求。snapshot行情／图表／私人历史各保留独立用途，不能因新方案重建K线流水线。
- [x] 当前与订单后净值／风险分别展示，报价参考价与均价取同组输入；库存等只标提交时检查。旧本金倍数／融资确认使用本地清算净值和资金缺口，但行情／账户改变不自动授权更大融资或跨零。
- [x] net提交保留idempotency_key、min_out、expected_economic_version、allow_growth；全部回补仍原cover_all契约，本地预估生成max_gold_in。未知结果继续原入口、完整参数和编号；回执先显示成功再协调刷新，失败不能变成重复下单。
- [x] LMSR每笔成交后走共享聚合刷新，SSE gap／恢复统一交给协调器；当前行情仍通过原图表流处理成交量，仅一个定价owner更新交易risk。旧summary字段的兼容映射明确是最近基准，不能写入approx结果当成服务端权威；其他页面保持现有行为。
- [x] 通知显示与大刷新独立：新事件立即提示，ready恢复后允许正常交易；stale/unknown不显示健康，纯减仓保留原服务端路径。断线、冻结、费率更新、没有持仓、手机窄屏文案沿用现有设计。
- [ ] 复用pending行为测试覆盖成功后刷新失败、账号切换、旧spot／borrow-buy／short请求恢复和纯平仓意图；仅新增缺失真实用例，不做template文本断言。
- [x] frontend执行 `npm run test:unit`、`npm run type-check`、明确列出改动文件的只读ESLint、`npm run build`；必要截图覆盖正常报价、跨零、暴跌预警、账户同步中、断线与手机。不自动点击／填表／交易。

**完成条件：** 玩家输入和价变即时看到本地结果，账户通知自动同步；原资金保护、旧请求恢复和图表行为未放宽。

## Task 6：闭环验收、文档与交付准备

**依赖：** Task 1–5。**Files:** 新 `docs/browser-local-trading-validation-<实际日期>.md`；更新本spec／plan、`docs/api.md`、`docs/architecture.md`、`docs/README.md`。只修复本轮实际发现的问题，不机械新增验收工程。

- [x] 用spec验收逐项对照本计划结果：初始化跨版本、两种净值、全组清算、借款／强平／管理员通知、本人成功响应合并、末条事件／行情遗漏、rule生效、凭证失效、旧pending、未知数值和纯减仓。不存在API聚合后仍每帧summary／net quote的旁路。
- [x] 一次小范围隔离探针记录首次context、无变化版本检查、五组行情输入、一次账户事件／事件簇的实际HTTP/SQL数和事件循环耗时。负载只用临时库和匿名固定数据，不扩展全站压测、不新增墙钟性能永久断言、不把一个API说成一条SQL。
- [x] 运行本轮涉及的保留后端用例和PG独立竞态检查；完成项目要求的Python编译／应用import／pytest -x（`DATABASE_URL`指向新/tmp SQLite，PG用独立库；import可能需要ownership/config测试环境，先安全准备）。前端unit／type-check／只读lint／build只在新改动或未决风险下追加，记录实际最终版本，不能复用改动前结果冒充。
- [x] 记录真实命令、成功／失败／skip、已知警告、截图和未验证范围。文档阶段通过、mock检查与API吞吐均不能称为完整浏览器产品验收；不自动启动交互流程。
- [x] 自查完整diff只含本任务，原用户改动保留；更新验证报告与任务勾选，独立只读整段审阅并修复重要问题。按后续明确授权进行提交／PR／合并／推送／部署，当前不自行发布。

**完成条件：** 需求与实际证据对应，遗留风险清楚；不以用例数量当作产品验收。

## 顺序、职责与自查

`Task 0（确认门槛）→ Task 1 → Task 2 → Task 3 → Task 4 → Task 5 → Task 6`。Task 3在输入和数值合同冻结后可独立准备；若后来选择委派，后端读协议、commit事件、前端纯数学、会话／页面应分别明确文件所有权，交叉写入口由主agent协调，子agent不得改技术路线。

计划自查（文档阶段）：spec §1–5对应Task 0–2，§6对应Task 3，§7–8对应Task 4–5，§9–10对应文件职责和Task 6，§11确认门槛对应Task 0。Review Focus五项均有所属行为验证；接口名／版本域跨任务一致；测试集中在生产读一致性、提交事件、纯数学、会话恢复四个失败层，不在每层复制完整资金场景。定向核对补清 SSE 代理前缀、兑换／消费真实 managed_transaction owner、原价帧混用限制、8dp exact-output 费率及初始化 context 暂存后安装，未将历史注释当作当前实现。

本计划是可审阅的执行草案；未确认Task 0具体技术路线前不进入产品实现。没有为了写plan创建工作树、安装依赖、运行产品测试或改产品代码。

精度与200ms／15s／1s已于2026-10-12补充确认。固定常量命名为 ACCOUNT_REFRESH_COALESCE_MS=200、TRADING_VERSION_CHECK_MS=15000、LOCAL_INTEREST_TICK_MS=1000（可由负责会话的模块集中导出）；文档说明用途，不做控制台可配置项。200ms合并账户事件后的完整刷新，15s只核对版本并按需刷新，1s只是浏览器本地计息重算，不发网络请求。首次加载、本人成功操作或手动刷新不必等待事件合并窗口。

## 2026-10-12实施证据状态

Task1–5 medium复审均无未解决Critical／Important；勾选表示实现与相应阶段检查已记录，不表示浏览器产品验收。未勾选项保留具体缺口：Task2每类owner真实提交事件矩阵、排队期间第二次提交；Task5旧spot本来没有持久pending恢复存储，未新增完整恢复；Task6指标已记录，合成提示／手动刷新不代表自动合并；最终整段审阅及后续授权结果见末节。详细命令、警告／skip、mock与实际HTTP区分及未验证范围见[验证记录](../../browser-local-trading-validation-2026-10-12.md)。发布遵循末节的后续明确授权。

## 最终复审与发布授权

最终整段medium审阅发现未知估值／初始门槛仍显示健康，已在共享风险状态修复；45项定向检查、type-check、限定只读ESLint通过，单次复审PASS，无未解决Critical／Important。此前完整前端检查242项及build早于该最后纯helper修复，后端未变。五项Minor与未覆盖场景继续在验证记录披露。用户后续明确授权提交、推送并触发现有自动部署，覆盖本计划原实施阶段不自行发布的限制；按现有push main CI/CD执行，不调整部署架构。
