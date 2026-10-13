# FX 到价挂单 Implementation Plan

> **For agentic workers:** 执行时按 superpowers:executing-plans 逐项推进；仅在用户明确选择委派时使用 superpowers:subagent-driven-development。用户于 2026-10-14 授权按完整 spec/plan 开始实施：Sol low 执行、Sol medium 审阅；涉及新算法或新功能必须停下向用户报告。执行者须同时阅读 spec 和用户 AGENTS.md，技能不得扩大任务或测试范围。

**Goal:** 玩家用填空条件创建最多 3 张 FX 挂单，到价后调用与前端下单共用的成交逻辑，并在交易页面收到成功或失败通知。

**Architecture:** 一张挂单表、现有 APScheduler 和私人 trading SSE。统一买卖提取最小事务内核心，网页包装器保留既有行为；挂单包装器在同一事务保存成交和挂单成功结果。借款缺口及平仓数量由共用成交业务层按执行时锁内状态计算，不复制算法。

**Tech Stack:** Python / FastAPI / SQLModel / SQLAlchemy / PostgreSQL / Alembic / APScheduler；Vue 3 / TypeScript / Pinia / Naive UI，沿用当前依赖版本。

**Spec:** [FX 到价挂单设计](../specs/2026-10-14-fx-trigger-orders-design.md)。独立审阅来源：[Sol medium review](../reviews/2026-10-14-fx-trigger-orders-sol-review.md)。

## Global Constraints

- 每用户全账户最多 3 张有效单；不预留资金或额度，无到期时间。
- 行情 `price <= trigger_price` 或 `price >= trigger_price`，到价尝试一次逻辑交易；不保证均价，不添加玩家滑点参数，不追溯扫描间触价。
- 默认扫描 10 秒，可由管理员修改；每轮批量检查全部有效单，按 ID 顺序处理候选，任务不重叠。
- 买入按金圆券金额，卖出按外币数量，允许跨零；平仓只减仓。自动金圆券借款默认关闭，开启时受用户上限及现有风控约束。
- 明确失败结束并释放槽位；到价时市场暂停或 FX 总闸关闭也失败。直接下单的既有暂停减仓规则不变。
- 前端和 scheduler 共用成交逻辑及资金助手，不通过 HTTP 自调用，不复制报价/借贷/账务，不关闭网页已有版本及成交保护。
- 结果提交后通过私人 SSE 通知，只有交易页面接收；离线查看挂单记录。不加通知表、补发、消息总线、恢复队列或通用订单框架。
- 本计划的新表/API/模块、局部成交事务接缝和推荐规则随用户“开干”获准实施；后续新算法/功能/架构变化须先报告。此计划不授权 push、合并、部署或生产操作。
- 保留工作区已有文档及截图；执行时使用隔离 worktree。若委派，各任务明确文件所有权、不覆盖其他人的修改、不自行扩大架构/权限。
- 新永久测试只覆盖真实新增行为/资金不变量缺口，复用现有夹具。UI 默认只截图，不自动点击/填表/交易。局部改动不反复全套验证。

## Review Focus

1. 自动买入跨零：先回补及释放锁金，再算新多头融资，不能把全部预算直接减现金（Task 1）。
2. 空头 100% 平仓：包括执行时利息，不因挂单时数量过期留下残余，不开反向多头（Task 1/3）。
3. 第四张单与撤单/成交竞争：3 槽及终态真实有效，撤单成功后不成交（Task 2/3，PG 只验证这一独立锁风险）。
4. 成交和结果共同回滚、响应未知：成功必须有真实回执；固定请求身份，不换编号重做（Task 3）。
5. 私人通知及生命周期：失败也通知、成功不重复弹账户提示、账号隔离，reset 不留下旧单（Task 2/4/5）。

## 文件与职责

| 文件 | 责任 |
| --- | --- |
| `backend/app/services/fx/net_trading.py`、`shorts.py` | 共用成交核心，动态授权解析及已有账务助手复用 |
| `backend/app/models/fx.py`、`schemas/fx.py`、新 Alembic revision | 挂单持久化与参数/响应合同 |
| 新 `backend/app/services/fx/orders.py` | 创建、查询、撤单、单张触发执行与结果 |
| 新 `backend/app/services/fx/order_scheduler.py` | 批量扫描、调度启动/关闭/改频 |
| `backend/app/api/v1/fx.py` | 本人订单 API |
| `backend/app/services/site_config.py`、`api/v1/site_config.py`、`main.py` | 默认配置及现有生命周期接入 |
| `backend/app/services/account_events.py` | 复用本人队列发送结果事件 |
| `backend/app/api/v1/admin_fx.py`、`backend/scripts/season_reset.py` | 市场删除限制与赛季清理依赖 |
| `thccb-frontend/src/types/fx.ts`、`api/fx.ts` | 前后端挂单合同 |
| 新 `thccb-frontend/src/components/market/FxTriggerOrders.vue`、`pages/Fx.vue` | 挂单表单、列表、撤单及结果记录 |
| `thccb-frontend/src/stores/tradingSession.ts`、`types/tradingContext.ts` | 接收结果并通知、刷新；保留身份隔离 |
| `thccb-frontend/src/pages/admin/SiteConfig.vue` | 扫描间隔配置控件 |

## Task 0：实施前确认与基线

**产物：** 确定待确认规则和执行环境，不写功能代码。

- [x] 提交完整 spec/plan 给用户确认：新增表/API/两个业务模块及最小事务核心调整；平仓固定方向、触发时持仓比例；有挂单历史只允许归档及永久不可用终止。不能把“写 plan”当作这些事项已经获批。
- [x] 将本文推荐配置明确为实现选择：`fx_order_scan_interval_sec` 默认 10，取值 5–3600 秒，沿用现有配置保存和即时 reschedule；3 槽固定，不增加槽位配置。超出 spec 的业务选择等用户决定，例行数值校验不另造系统。
- [x] 执行时读取 worktree 技能并建立隔离环境，记录基线 SHA 和既有改动；当前主工作区文档不覆盖。确认运行时实际 migration head，以现有 head 新增增量 revision，不修改 frozen baseline。
- [x] 保留用户选定的执行方法；本计划默认顺序实施，因为核心事务与 orders 共用文件不能并行修改。

## Task 1：前端与挂单共用成交核心

**Files:** `backend/app/services/fx/net_trading.py`、`shorts.py`、`schemas/fx.py`；保留并扩展 `backend/tests/test_fx_net_trading.py`、`test_fx_short_trading.py`。

**Interfaces:**

- `execute_net_trade(...)` 和 `execute_short_cover(...)` 原公开签名/响应继续支持网页调用。
- 新 `execute_net_trade_in_session(db: AsyncSession, *, user_id: int, pair_id: int, req: FxNetTradeRequest, credit_deps: DependencySet | None) -> FxNetTradeResponse`：调用者已持门闩和事务，函数不 commit/rollback/publish。
- 内部 `FxTradeInstruction`（放现有 FX schema 模块）：`action: buy|sell|close_long|close_short`、`amount: Decimal | None`、`close_percent: Decimal | None`、`auto_borrow: bool`、`borrow_limit: Decimal`。不包含挂单条件、槽位或 UI 状态。
- 新 `execute_fx_instruction_in_session(db: AsyncSession, *, user_id: int, pair_id: int, instruction: FxTradeInstruction, request_key: str, credit_deps: DependencySet | None) -> FxNetTradeResponse | FxShortExecution`：将授权指令按锁内状态解析后，调用事务内资金核心；该函数也位于现有成交模块，不另建计算框架。
- 既有 `execute_short_cover_in_session` 保持网页固定数量/金额上限合同；增加内部调用所需的比例/锁内报价路径，避免传过期网页参数。具体私有参数可由实现者局部选择，不开放未要求的新玩家 API。

- [x] 在提取前运行现有 net/short 相关用例保存基线。复用原 fixture，新增最小缺口断言：自动借款上限内借实际差额、超上限无资金变动、跨零释放锁金后只融资新多头；不复制 AMM 算法推导预期。
- [x] 提取现有执行体，门闩和重试仍由包装器管理；身份回放、锁内模拟、成交回执及审计复用。网页包装器捕获变化描述后 commit，再做原发布，返回值/异常/请求身份保持兼容。
- [x] 指令解析在同一 UTC T 和真实锁内状态完成。融资接入 `_simulate` 的回补后新多头段，实际缺口为该段预算减其可用现金且不低于零；超 `borrow_limit` 明确拒绝。固定借款网页请求继续按原授权金额，不偷偷变成自动借款。
- [x] 多头比例平仓按当前持仓向下量化并执行卖出、禁止增仓；空头先含息确定比例，100% 使用 `cover_all`，部分精确回补复用同一资金助手。不存在对应方向持仓或量化为零返回明确无持仓/数量不足结果，不造零交易。
- [x] 验证事务内调用后主动回滚，账户、池子、持仓、成交/回执/审计全无变动；保留已有跨零原子性和回放有效断言。真实调用助手，不用 mock 的 commit 次数证明原子性。
- [x] 运行 `cd backend && venv/bin/python -m pytest -q tests/test_fx_net_trading.py tests/test_fx_short_trading.py tests/test_fx_short_audit_replay.py`。预期全部相关行为通过。只提交本任务文件，独立审阅仅在用户已选该执行方法时进行。

## Task 2：挂单存储、本人 API 和删除/reset 接入

**Files:** model/schema/new revision、`orders.py`、`api/v1/fx.py`、`admin_fx.py`、`backend/scripts/season_reset.py`；复用 `test_credit_migration.py`、`test_fx_season_reset.py`、`test_fx_net_trading.py`，新增集中 `backend/tests/test_fx_trigger_orders.py`。

**Interfaces:**

- `FxTriggerOrder`：`id/user_id/pair_id`；`comparison: lte|gte/trigger_price`；Task 1 指令字段；`status: waiting|succeeded|failed|cancelled`；`created_at/finished_at/observed_trigger_price/failure_reason`；可空 `execution_id` 和 `trade_id`（统一成交/独立回补二选一结果引用）。稳定内部请求 key 为 `fx-order:{id}`，不保存不断变化的网页预估参数。
- `create_order(db, user_id: int, req: FxTriggerOrderCreate) -> FxTriggerOrderRead`。
- `list_orders(db, user_id: int, *, status: str | None, before_id: int | None, limit: int) -> list[FxTriggerOrderRead]`，limit 默认 50、上限 100。
- `cancel_order(db, user_id: int, order_id: int) -> FxTriggerOrderRead`；已取消返回同结果，其他终态不能改为取消。
- `POST /fx/orders`、`GET /fx/orders`、`POST /fx/orders/{order_id}/cancel`，复用现有 fx router 本人身份；无管理员代挂单/修改订单接口。

- [x] 建表及真实升级/回滚：旧现金、持仓、成交及回执不变；FK、状态/数值/指令组合约束正确。非空挂单表 downgrade 拒绝删业务记录，空表可回滚。测试不固化“该 revision 永远最新”。
- [x] 创建时只检查参数、币种可见性和身份/槽位，不承诺触发时资金充足；创建必须在 User 行锁下数 waiting 再插入。price/amount 使用现有六位 Decimal，百分比 `(0,100]`；自动借款关闭时上限为零，卖出/平仓不能带金圆券融资授权。
- [x] 创建如需读/锁 pair，先于 User；撤单采用 User → order。禁止持 User/order 再申请 GATES。查询、撤单不可泄露其他玩家数据。
- [x] 按 Task 0 确认的删除政策实现：推荐任何挂单历史存在就拒绝硬删除并提示归档；归档/永久关闭订单处理由 Task 3 扫描集中完成，不在管理事务里遍历所有用户锁。
- [x] reset 清理先 order，再 FX 成交/回执与池相关子表、最后 pair；检查实际现行 FxPoolEvent、FxExecution 引用，修正本轮新增依赖所必需的清理顺序。business reset 沿用 metadata/head 空业务校验，只修真实新表接入缺口。
- [x] 集中 orders 测试调用真实服务/HTTP：3 张成功、第 4 张拒绝、撤销后可再创建、他人不可见/不可撤销、分页历史终态保留。migration/reset 分别只在原层验证，不重复整套交易场景。
- [x] 运行 `cd backend && venv/bin/python -m pytest -q tests/test_fx_trigger_orders.py tests/test_credit_migration.py tests/test_fx_season_reset.py tests/test_season_reset.py`。检查新增 revision 与 metadata 兼容；只提交本任务改动。

## Task 3：单张触发执行与结果原子性

**Files:** `orders.py` 与 Task 1 成交模块必要接缝；`test_fx_trigger_orders.py`，现有 PG helper/一次性库，必要新增 `backend/tests/pg/test_pg_fx_trigger_orders.py`。

**Interfaces:**

- `attempt_order(order_id: int, observed_price: Decimal) -> FxTriggerOrderRead | None`：独立会话和写事务，调用共用核心；None 表示已撤销/已有终态，不能重复交易。
- `FxOrderResult` 私人事件 DTO：`order_id/pair_id/currency_code/action/status/occurred_at`，成功成交金额/实际借款/结果引用或失败原因；不带其他玩家信息或池隐藏参数。

- [x] 候选确定后不再按新价格改回 waiting。取得已有有序 GATES、pair、User，再 order 行锁重查 waiting；校验用户 active、暂停/归档/总闸，按订单授权构造 Task 1 指令。任何借款/数量/版本读取不得靠挂单时页面快照。
- [x] 共用核心写账后，写订单 succeeded、触发观察价格及回执引用，同事务 commit。资金事件描述和通知在 commit 后发布，持门闩直到提交；失败不留下半笔跨零成交。
- [x] 明确业务异常先回滚资金，再在 User → order 锁下重查 waiting，记录 failed/原因/触发价/结束时间；若期间撤单成功，不覆盖 cancelled，也不发虚假失败通知。
- [x] 固定 key，不针对一次挂单生成新的成交身份。遇提交异常结束本轮，恢复处理先读订单和固定 key 回执；有成功证据只认定成功，不重算不同参数执行。若订单结果与回执不一致，日志报警并停止扫描，人工核对；不造未知终态/队列/自动恢复表。无提交证据且事务已明确回滚时记录失败；不能把数据库不可读当成业务失败。
- [x] 测试分别证明实际可观察结果：条件触发买卖/跨零；资金不足/超授权/暂停/总闸失败并释放槽位；100% 含息空头全平；重复执行同订单只有一笔逻辑成交；实际外层回滚后既无成交也无成功结果。断言真实余额/债务/仓位/回执，复用既有 fixture。
- [x] PG 仅补 SQLite 不能证明的独立锁行为：并发创建不超过 3、撤单与触发只有一个结果，资金和终态一致。复用现有 ownership/GATES 和 test-only URL 守卫，先校验一次性库名再建表，不访问业务库。
- [x] 运行 `cd backend && venv/bin/python -m pytest -q tests/test_fx_trigger_orders.py tests/test_fx_net_trading.py tests/test_fx_short_trading.py`；在明确一次性 `TEST_PG_DATABASE_URL` 下运行 `venv/bin/python -m pytest -q -m pg tests/pg/test_pg_fx_trigger_orders.py`。未运行 PG 如实记录，不能用跳过算通过。

## Task 4：扫描、管理员配置与私人结果通知

**Files:** `order_scheduler.py`、`site_config.py`、`api/v1/site_config.py`、`main.py`、`account_events.py`；复用 `test_site_config.py`、`test_site_config_api.py`、`test_trading_stream.py`，orders 行为仍集中原新增文件。

**Interfaces:**

- `run_order_scan_once() -> dict[str, int]`：返回 checked/triggered/succeeded/failed 统计，不记录私人明细到公共输出。
- `start_scheduler() / stop_scheduler() / reschedule(interval_sec: int)`，沿用现有 scheduler 生命周期。
- `enqueue_order_result(user_id: int, payload: FxOrderResult) -> None`，向已有 `EVENTS` 的该用户订阅 offer `fx_order_result`，失败只记录日志。

- [x] 批量投影读取所有 waiting 与所需 pair 的公开六位 `price`，复制基础值后结束读事务；比较包含等号。每轮按 order ID 处理观测达标者，调用 Task 3；不把池内储备/私有订单公开。
- [x] 无可靠行情本轮跳过。按 Task 0 确认的永久不可用策略记录终止；普通未达价 continued waiting。到价暂停/总闸不能在扫描前被过滤成永远等待。
- [x] 写实例获得 ownership 后启动，`max_instances=1, coalesce=True`；停止先停挂单生产者及等待本轮正在提交的动作，再按原顺序排空行情/释放 owner。不引入持久 job store或手动 run-now API。
- [x] 添加默认配置和验证，保存后 reschedule；不因管理员改频取消已经开始的资金事务。只读实例不能启动扫描或调用写执行。
- [x] 成功/失败结果持久化后发送本人事件，成功使用专用 account reason 保留刷新；无资金变动失败只发结果。现有 stream 原样透传，除真实需要不改其认证/重连协议。
- [x] 复用 HTTP SSE 夹具断言成功和失败事件仅本人收到；orders 用例验证不到价保留、到价失败结束、观察价在前笔改变池价后仍触发下一候选。已有配置测试验证默认/非法值/保存，scheduler 不用墙钟睡 10 秒测正确性。
- [x] 运行 `cd backend && venv/bin/python -m pytest -q tests/test_fx_trigger_orders.py tests/test_site_config.py tests/test_site_config_api.py tests/test_trading_stream.py`；只提交相关文件。

## Task 5：交易页面填空表单与结果展示

**Files:** 前述 frontend 文件；复用 `stores/__tests__/tradingSession.spec.ts`，新增确有缺口的挂单请求/表单行为用例集中一处，不增加截图断言测试。

**Interfaces:**

- fx API 增加 `createTriggerOrder/listTriggerOrders/cancelTriggerOrder`，字段与 Task 2 schema 一致，金额用现有 Decimal 字符串。
- `FxTriggerOrders.vue` props：`pairId: number`；从已有 trading session 取身份/仓位，自己管理挂单列表和表单，不拥有第二套账户/行情连接。
- trading session 增加只读 `lastOrderResult: FxOrderResult | null`；新事件更新此值并显示通知，成功请求账户同步，组件监听结果重新取列表。共享流中的所有币种结果都通知本人，不只当前 pair。

- [x] 在现有 FX 页面加入填空表单和全账户 `n/3` 槽位；按动作联动金圆券/外币数量或方向/比例。买入显示可选自动借款上限；卖出提示可能借币开空，平仓不反向开仓。沿用现有风格/组件，不重新设计主交易面板。
- [x] 展示全账户 waiting 和分页结果，保留币种标识；支持撤销等待单，修改通过撤销重建。不增加前端独立 scheduler。提交成功以 API 返回为准，失败清楚提示；不写通用请求恢复层。
- [x] 提醒“触发价不保证成交均价”“不预留资金”“定时检查，短暂触价可能错过”；现有公开读取路径不提供扫描间隔，实际秒数在管理员配置展示，不新增 API。所有技术参数名和内部异常不直接呈现玩家。
- [x] 成功/失败事件分别通知并刷新列表；挂单 reason 的 account_changed 跳过通用账户弹窗，但保留同步；事件去重沿用已有 event/订单身份，账号切换清空 lastOrderResult 和旧列表。只在已有交易页面流有效时接收。
- [x] 管理配置页增加扫描间隔输入，其他开关/交易规则不动。
- [x] 前端新增最小行为覆盖：失败事件也可见、成功只有一次专用提示且仍刷新、迟到旧账号事件不覆盖新账号；复用 tradingSession 用例，不以模板字符串断言文案或布局。
- [x] 运行 `npm --prefix thccb-frontend run test:unit -- src/stores/__tests__/tradingSession.spec.ts` 及本轮实际新增行为用例；`npm --prefix thccb-frontend run type-check`、`npm --prefix thccb-frontend run build`。截图核对桌面/移动端表单和列表，不自动点击/填表/交易。

## Task 6：集中验证与交付记录

**Files:** `docs/fx.md`、`docs/api.md`、`docs/rules-raw.md`、`docs/README.md`；新 `docs/fx-trigger-orders-validation-<实际日期>.md`，本 spec/plan 状态。

- [x] 更新 FX 规则、API 和索引：仅 FX 新增到价挂单，LMSR 仍即时成交；删除规则书中“全站暂不提供条件单”的失真描述。说明触发价/均价差别、3槽、借款授权、失败终止、通知范围和扫描可能漏过短暂触价。
- [x] 最后相关代码变化后集中运行项目必要检查：`cd backend && venv/bin/python -m compileall -q app`、`venv/bin/python -c 'import app.main'`、`venv/bin/python -m pytest -x`。前端全量 `npm --prefix thccb-frontend run test:unit`、type-check/build 如 Task5 之后无变更复用实际通过证据，不重复运行。PG 用 Task3 有效证据，不穷尽新的故障矩阵。
- [x] 检查真实新 schema 升级及保留数据、删除/reset 接入、共享成交网页兼容、暂停例外与本人通知。没有新的具体风险时结束验证；不追加生产混合压测或自动浏览器交互。
- [x] 记录实际命令、结果、PG/截图范围、既有警告及未验证部署/生产项；任何全量失败先定位具体层，不删除有效断言或放宽预期。最终审阅状态单列如下，不把前述检查当作审阅通过。
- [x] 只提交本任务文件，交付分支及 reviewable diff；推送/PR/合并/部署需后续授权，不能把实现计划当作发布授权。

- [x] Task6 及全分支 Sol medium 最终审阅：Ready；`bfaeaa7` 局部文案修复 scoped Approved，无剩余 C/I/M，见[最终审阅记录](../reviews/2026-10-14-fx-trigger-orders-final-review.md)。

## 计划自查与执行交接

spec 基本原则对应 Task1/3；动作/融资 Task1/3/5；存储/API/删除/reset Task2；扫描/配置/生命周期 Task4；SSE Task4/5；文档与验证 Task6。测试分层覆盖独立风险，不逐层复制全部成交场景。

用户已确认完整方案及全部推荐细节，选择 Sol low 顺序实施、Sol medium 逐任务审阅。Tasks0–6 已完成；全分支最终 Sol medium 审阅 Ready，局部文案修复 `bfaeaa7` scoped Approved，无剩余 C/I/M。仅获本地实施授权，未推送、合并或部署。实际证据见[验证记录](../../fx-trigger-orders-validation-2026-10-14.md)。
