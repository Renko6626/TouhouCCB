# FX 统一多空处理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking. 用户已批准技术路线并指定一个sol medium实现者；在隔离工作树执行Tasks 1–6。

**Goal:** 统一玩家买入／卖出，反向先平已有仓位，允许单请求跨零开仓且全部成功或全部回滚；限价单后置。

**Architecture:** 沿用现有 AMM、钱包／欠币模型、统一风控和单写所有权，增加薄的整笔报价／执行编排。技术提案为新增统一 API 和 FxExecution 成功回执，将一至两条已有 purpose 的真实成交归入一个幂等请求；接口及存储调整已确认。

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy / SQLModel / PostgreSQL / SQLite / Vue 3 / TypeScript；不增加依赖或部署组件。

**Spec:** [2026-10-11-fx-net-trading-design.md](../specs/2026-10-11-fx-net-trading-design.md)。已确认业务原则与待确认技术提案分别见设计 §1、§2。

## Global Constraints

- 不改变 AMM、手续费、计息、保证金公式；不将手续费设为零，不引入净额授信。
- 不把钱包改为负数，不允许成功提交后的同 pair 多空并存。
- 反向先平仓，允许跨零，后段失败整笔回滚；旧 API 不自动扩展成跨零交易。
- Decimal 六位金额、字符串接口、既有范围校验；第二段使用第一段成交后的储备。
- 一次取得完整有序 GATES；品种先于 User；等待不占 DB 连接；有限重试须先全回滚释放。
- 减仓沿用既有允许规则，增仓按整笔最终状态做一次组合准入；融资必须明确同意，仅支持新多头段。
- 不默认给无债现金路径增加全组合估值；不随每个 SSE 帧重新估值。
- 限价单后置；本计划不创建挂单表、资金预留、挂单任务或新计算服务。
- 不覆盖工作区既有文档／截图；迁移仅在可丢弃测试库验证，不提交、推送或部署。
- 新增永久测试只覆盖本功能真实资金／请求身份缺口，复用已有夹具。UI 最多截图，无自动点击／填表。

## Review Focus

1. 第二段因库存、门槛或 dust 失败时，第一段、贷款、利息与审计必须同时回滚（Task 3）。
2. 同编号并发、响应丢失及跨旧入口重试只返回一次完整结果，不重复任意一段（Task 2、5）。
3. 净仓位跨零与含息欠币边界使用实际 Decimal 后态，不遗漏尾债或同时留下正钱包／欠币（Task 1、3）。
4. 冻结／reduce-only／贷款关闭仍允许原有纯减仓，但不允许偷偷反向开仓或借新金债回补（Task 3）。
5. 一请求多腿不得重复行情成交量，旧成功记录不得被新请求参数解释为另一种动作（Task 2、4）。

## 文件与职责

| 文件 | 职责 |
| --- | --- |
| 新增 `backend/app/services/fx/net_trading.py` | 只读整笔模拟、锁外发现／锁内整笔执行、回执和提交后通知；不复制 AMM |
| `backend/app/services/fx/trading.py`、`shorts.py`、`borrow_buy.py` | 局部提取现有结算逻辑供编排复用，保留旧入口契约 |
| `backend/app/models/fx.py` 与新 Alembic revision | 最小成功回执和实际成交关联；不创建挂单存储 |
| `backend/app/schemas/fx.py`、`api/v1/fx.py` | 新的认证统一接口；旧请求与公开行情兼容 |
| `backend/app/services/credit/risk.py` | 校验最终模拟状态及一致的储备；不更改门槛公式 |
| `backend/app/services/audit_service.py`、`audit_replay.py` | 请求关联元数据，各腿仍按真实 purpose 回放 |
| 新增 `thccb-frontend/src/api/fxNetTrading.ts`、`composables/useFxNetQuote.ts` | 统一契约、350ms 防抖、账号绑定请求恢复 |
| `thccb-frontend/src/pages/Fx.vue`、`types/fx.ts`、`utils/fxPresentation.ts` | 接入拆分报价、整笔回执和私人历史；复用现有布局／金额工具 |

## Task 0：固定技术提案与实施基线

**Files:** 本计划、配套设计；只读检查现有 Git、模型与调用方。

- [x] 用户确认设计 §2：新增统一入口、买金圆券预算／卖外币数量、显式融资仅用于新增多头，以及成功回执关联真实腿的存储方案。
- [x] 固定实施 SHA，记录用户现存改动；进入执行时按工作树技能创建或确认隔离工作区。计划中的旧记录兼容要求不能通过清库满足。
- [ ] 生产存量同pair正钱包/正含息欠币未预检：不为勾选而读取生产库。隔离库已覆盖异常阻塞；未做净额迁移。

**交付证据：** 已确认的契约与实施基线。未完成此任务，不执行后续产品代码或 schema 改动。

## Task 1：只读整笔拆分与报价

**Files:** 新增 `services/fx/net_trading.py`；修改 `schemas/fx.py`，必要时局部提取 `trading.py`、`shorts.py` 的纯报价／金额辅助；新增精简 `tests/test_fx_net_trading.py`，复用 `tests/fx_test_helpers.py` 和既有信贷夹具。

**Interfaces:**

- `FxNetQuoteRequest(side, amount, borrow_amount=0)`；买入 amount 为含费金圆券预算，卖出 amount 为含费外币数量。
- `quote_net_trade(db: AsyncSession, user_id: int, pair_id: int, req: FxNetQuoteRequest) -> FxNetQuote`。
- `FxNetQuote` 返回总输入／产出／同币种手续费、`legs`、预计方向／钱包／含息欠币、现金／金债／锁金变化、增仓量、可执行性及原因、pair 与账户版本、30 秒到期时间。每腿包含 purpose、输入／产出币种及金额、手续费、前后储备；不返回私有 ORM 对象。

- [x] 补行为用例：多头 100 卖 40／100／140 的实际分类为现货 40／100／100+开空 40；空头含息 Q 在预算低于、等于、高于完整回补成本时正确划分。断言真实报价金额、最终方向及数据库没有资金／审计变动。
- [x] 实现设计 §3 的序列报价，调用 `quote_sell`、`quote_buy`、`quote_buy_exact_out`；不复制恒定乘积推导。不以完整 Q 不可报价为由取消原本允许的部分回补。
- [x] 返回整笔 min_out 所需总产出和分段资金用途；有增仓时复用一次最终组合风险投影。只读报价不获取写门闩或 FOR UPDATE。
- [x] 覆盖非零费率、旧息、新多头 dust、Q 尾差，以及报价后态金额超存储范围；预期来自独立守恒／明确输入结果，不在测试中复制拆分算法。
- [x] 运行 `venv/bin/python -m pytest -q tests/test_fx_net_trading.py tests/test_fx_amm.py tests/test_fx_short_trading.py`，指定独立 SQLite `DATABASE_URL`；实际结果记录到验证文档。

**交付证据：** 一份可读的整笔报价，清楚说明各段数量、费用与阻塞原因，无资金写入。

## Task 2：最小成功回执、关联与全 FX 请求查重

**Files:** `models/fx.py`；新迁移（实际实现日命名、前驱取实施基线真实 head）；`services/fx/net_trading.py` 与旧写 wrapper；`tests/test_fx_migration.py`、Task 1 的行为用例。

**Interfaces:** `FxExecution(id,user_id,pair_id,idempotency_key,request_payload,response_version,response_payload,created_at)`，独立主键与 `(user_id,idempotency_key)` 唯一约束、用户／pair 外键；`FxTrade.execution_id`／`execution_leg`。`FxNetTradeResponse` 包含 `execution_id, trade_ids, legs, input_amount, output_amount, fee_amount, post_price, created_at, replay` 及资金拆分；金额序列化成字符串。

- [x] 在现有迁移用例中覆盖旧数据保留、外键／唯一约束，以及无统一成交时可回滚；有统一成交时拒绝会破坏请求身份的降级。不得断言某 revision 永远为最新。
- [x] 创建回执与关联列。新请求腿保持 `spot/short_open/short_cover/borrow_buy` purpose，旧记录和公开输出不改写。
- [x] 在 User 锁内共享请求查重，先检查原成功回执／旧成交再处理本次限制；相同编号不同参数或跨入口请求返回 409。保存原 min_out、融资参数和请求类型，不按新仓位重新拆分旧请求。
- [x] 新旧 wrapper 接入同一编号命名空间；新腿不使用用户编号或派生编号，关联腿约束防止重复。
- [x] 复用已有回放场景，验证历史编号可回放、参数冲突无变动、回执内所有腿一致且不会产生只提交一腿的记录。

**交付证据：** 原子且可重放的一请求多腿身份；旧成交和迁移数据完整。

## Task 3：整笔成交、跨零、显式融资与资金边界

**Files:** `services/fx/net_trading.py`、`trading.py`、`shorts.py`、`borrow_buy.py`，必要的 `credit/risk.py` 储备后态兼容；扩展 `tests/test_fx_net_trading.py`，保留原 FX／loan／credit 检查。

**Interfaces:** `execute_net_trade(db: AsyncSession, user_id: int, pair_id: int, req: FxNetTradeRequest) -> FxNetTradeResponse`；请求为 Task 1 参数加 `min_out,idempotency_key,expected_economic_version`。编排层只 commit 一次，底层辅助不 commit、不发布。

- [x] 提取现有资金结算辅助，旧入口继续执行原 guards 和原风控；不要用暴露给客户端的 bypass 参数跳过准入。
- [x] 发现依赖、一次取门闩、锁 pair/User 与相关钱包／空头／treasury、查重、统一 T 计息、重报价并模拟整笔。新风险按最终组合检查一次，纯减仓按原允许语义执行。
- [x] 成功回放先于 expected_economic_version 检查；新请求的账户版本不同于报价则拒绝重报。覆盖报价后仓位被其他请求改变，原预计平仓不能静默变成新增空头／多头；池价格变化仍按整笔 min_out 重验。
- [x] 实现现货卖出优先还金债，随后只为不足的外币借币开空；两段手续费和锁金分开入账。
- [x] 实现先回补含息欠币，再用剩余预算买现货；本仓锁金只能用于本仓回补。显式金圆券借款只记入新增多头段，不为回补补资金；新本金从 T 起息。
- [x] 同事务保存每段真实成交、贷款／还款、成本与收益基准、经济版本、审计和成功回执；commit 前物化响应，异常整笔回滚。
- [x] 覆盖第二段库存不足／保证金不足／dust、融资失败的完整回滚；纯减仓在相关新增风险开关关闭或信用冻结下仍按原规则可执行，跨零整笔拒绝。
- [x] 用真实持久数据断言现金、金债、钱包、含息欠币、锁金、treasury、储备和审计守恒；commit 后同 pair 不同时存在正钱包／欠币。不改变估值与保证金公式。
- [x] 运行 `venv/bin/python -m pytest -q tests/test_fx_net_trading.py tests/test_fx_trading_in_session.py tests/test_fx_borrow_buy.py tests/test_fx_short_trading.py tests/test_fx_liquidation_service.py tests/test_credit_risk.py`，使用独立 SQLite 库。

**交付证据：** 真实数据库中的跨零原子成交、拒绝回滚和资金用途边界。

## Task 4：认证 API、历史／审计与行情接线

**Files:** `api/v1/fx.py`、`schemas/fx.py`、`services/audit_service.py`、`services/audit_replay.py`；相关 FX API／audit／candle 已有测试。

**Interfaces:** 设计 §2 的 `/net/quote`、`/net/trades`，消费 Task 1–3 契约；私人历史增加可空 execution 关联，公开 schema 不暴露请求参数／个人融资信息。

- [x] 接入认证与请求金额校验，错误保持明确的 4xx／可重试状态；拒绝未经认证的统一报价读取私人账户。
- [x] 私人历史按请求关联真实腿，审计保留每腿已有 purpose、资金快照及储备序列；无额外汇总 FxTrade。
- [x] commit 后只唤醒一次 FX market-data consumer，公开帧采用最终价格；consumer 仍读取全部真实腿，回放和失败不新增发布。
- [x] 在最合适的已有 HTTP 用例中验证字符串金额、认证、两段回执与隐私；行情用例验证两段真实成交量恰好计入一次。不要在 HTTP 层重复全部资金场景。
- [x] 运行相关 `test_fx_api.py`、`test_fx_audit_replay.py`、`test_fx_short_audit_replay.py`、`test_fx_candle_pipeline.py`、`test_fx_market_data_integration.py`，使用独立库。

**交付证据：** 新旧 API 兼容、审计可回放、公开行情无重复计量。

## Task 5：前端统一买卖与未知结果恢复

**Files:** 新增 `api/fxNetTrading.ts`、`composables/useFxNetQuote.ts`；修改 `types/fx.ts`、`pages/Fx.vue`、`utils/fxPresentation.ts`；复用现有 pending request 工具和展示／账号隔离单测。

**Consumes:** Task 1–4 固定 schema，不自行推断“做空必须借币”或“买入必须进入钱包”。

- [x] 常规买卖接统一入口，保留当前输入单位及显式融资。展示各段数量、整笔费用、最终方向、新增金债／欠币与锁金，不显示内部数据库概念。
- [x] 保留全部卖出／全部回补的纯平仓语义；关闭新增风险时报价仍允许有效的纯减仓，不用 side=buy 一刀切禁用回补。
- [x] 350ms 防抖获取只读报价，输入／账户／币种变化废弃旧结果，过期后不可直接提交；行情更新不触发每帧全组合重估。
- [x] 写请求前按账号持久化完整参数与编号；结果未知时保留，同编号恢复得到完整回执。旧 pending 现货／融资／空头请求继续调用其原入口恢复，不改路由重发。
- [x] 成功回执与账户刷新分开；私人历史展示“平多后开空／回补后做多”等真实结果。持仓均价、盈亏和快速平仓继续用后端实际剩余成本／含息数量。
- [x] 在已有请求恢复／展示测试中覆盖拆分展示、未知结果重挂载、账号切换和成功后刷新失败；不新增模板／样式字符串测试。
- [x] 运行 `npm run test:unit`、`npm run type-check`、改动文件的只读 ESLint 和 `npm run build`。必要截图覆盖多头卖出跨零、空头买入跨零、纯平仓及手机；不自动点击／填表。

**交付证据：** 同一操作可明确预览平仓与开仓，未知结果不会生成第二次交易。

## Task 6：定向并发验证与交付

**Files:** 扩展 `tests/pg/test_pg_fx_short_concurrency.py`；更新 `docs/api.md`、`docs/fx.md`、统一信贷说明中同 pair 操作描述；新增实施后的验证记录。

- [x] 用可丢弃 PostgreSQL 库覆盖同编号同时跨零只成交一次，不同编号竞争同一仓位重新分类，以及旧入口与统一入口争用同编号；保留现有锁序和冻结场景。
- [x] 执行 `TEST_PG_DATABASE_URL=<独立临时库> venv/bin/python -m pytest -q -m pg tests/pg/test_pg_fx_short_concurrency.py`；不要使用业务数据库。
- [x] 用 `/tmp` 临时脚本做一次小范围单段／跨零对照，记录 SQL 数、整笔耗时、同 pair 门闩等待及风控次数。验证不重复整套全组合估值、不在锁内同步广播；不扩展全站压测或给出未经测量的容量承诺。
- [x] 运行改动涉及的后端已有检查和 Task 5 前端检查，记录实际命令、结果和未验证项；新增永久场景优先只在一层覆盖。
- [x] 自查设计各条是否对应已完成行为，确认报价／执行／回放金额一致、旧 API 未发生隐式跨零。更新业务与接口文档；提交、迁移和部署按后续明确授权处理。

**完成条件：** 统一买卖和跨零原子性可验证，旧记录能回放，提交后仍单方向，费用／利息／锁金规则完整，公开行情不重复，未知请求可恢复。

## 后续阶段：限价单

统一多空交付后，再独立编写并确认限价单计划。复用统一交易的参数、报价、原子成交和请求编号，不另写一套资金结算。

- 不预留资金；条件未满足继续保留，满足后尝试一次逻辑交易，明确业务失败终止。
- 扫描基线为每 10 秒至多检查 50 张，单任务不重叠、公平轮转；条件检查和实际成交开销分开看。
- 每用户最多 3 张有效单为推荐值，届时固定计数范围和配置；执行中／结果未知仍占名额。
- 未知提交先核对原请求，不能重新编号重试；确认如何定义触发价与整笔成交保护。
- 该阶段才设计挂单状态／存储、撤单、到期、失败原因和 scheduler 生命周期。本计划不提前实现。

## 本次文档检查

Tasks1–6已实施并运行定向SQLite/PostgreSQL及前端检查；真实命令、结果、测量与未验证范围见[实施验证](../../fx-net-trading-validation-2026-10-11.md)。生产存量异常预检、生产迁移、部署及限价单未执行。
