# FX 机器人基础设施 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. 用户尚未选择实现委派方式；推荐主 agent 顺序实现，独立 reviewer 在完整改动后审阅。若用户选择 subagent-driven，再使用对应技能。Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有账户和真实 FX 现货体系上接入可配置的随机、库存调节、短线轮次机器人。

**Architecture:** 共用 User/BotProfile 和管理入口，新增独立 `pve_fx` 引擎及策略注册合同。机器人复用普通现货交易、审计和行情链路，短线状态与成交同事务提交；LMSR 引擎不做通用化改造。

**页面结构确认：** 保留 BotProfile 的 lmsr/fx 分支；“PvE 机器人”下设置 `/admin/pve/lmsr` 和 `/admin/pve/fx` 两个路由子页面，不改为两张profile表，也不用同组件内的本地分页承载两套管理业务。

**Tech Stack:** 现有 Python/FastAPI/SQLModel/Alembic/APScheduler/NumPy、Vue/TypeScript/Naive UI；不新增依赖。

**Spec:** [设计说明](../specs/2026-10-16-fx-pve-infrastructure-design.md)。实施前同时阅读设计、项目 AGENTS.md 和 research/fx/IMPLEMENTATION_HANDOFF.md。

## Global Constraints

- 只接入冻结的随机、库存调节、短线轮次策略；不接入 `research/fx/lmsr_borrowing/` 实验。
- 初始现金和初始外币在创建时自定义。5000／1000 仅是研究预设；库存基准等于该账户实际初始外币量。
- 一个机器人账户绑定一个 FX 交易对和一个策略，不让同一小号同时受 LMSR 与 FX 引擎管理。
- 真实成交复用现有普通现货内核。仅使用自有资产，不借钱、不借币、不通过统一净买卖入口隐式开空。
- 保存短线轮次所需的最小状态；不建设请求恢复系统、资金影子账、持久决策流水或额外 worker。
- tick 第一版固定 5 秒；管理页显示时间单位，不允许把它当作单户看盘间隔调整。
- 排行榜复用 `leaderboard_include_bots`；财富统计复用 `wealth_stats_include_bots`。不自动改变现有值，不新增 FX 专属隐藏机制。
- 生产代码不 import research 包，也不把模拟器的 Pool／Account 钱包复制进生产。
- 部署、迁移运行、初始资产实际发放和打开总开关在实现与检查完成后再交付审批。
- 新永久测试只对应新增关键行为和真实核心故障；不为表单、文案、注册常量或源码位置写测试。UI只截图，不自动点击填表。

## Review Focus

1. 自定义0现金、非零外币的账户：创建资产正确且不误判dead；归任务2、5的行为检查。
2. 停用账户／暂停profile／错误pair的内部调用：拒绝经济写，公共HTTP仍拒绝bot；归任务3。
3. 部分买回与负向轮次：不能超过剩余外币量，剩余成本与退出锁定保持；归任务4、5。
4. 初始外币发放后再真实买卖：池不因发放改变，审计回放连续，榜单过滤照常；归任务2、7。
5. season reset新增外键：清理顺序合法、资金按既有规则重置、FX bot总闸关闭；归任务1、7。

## 文件职责与依赖

|文件|责任|
|---|---|
|`backend/app/models/bot.py`、新 Alembic migration|市场分类、FX绑定、持久策略状态|
|`backend/app/schemas/pve_fx.py`|管理请求/响应及配置参数schema|
|`backend/app/services/pve_fx/types.py`|策略和执行边界的数据合同|
|`pve_fx/config.py`、`presets.py`|已验证交易对配置、冻结预设与个人系数|
|`pve_fx/accounts.py`|创建发放、估值、启停改参、清仓/退休条件|
|`pve_fx/registry.py`、`strategies/{random,inventory,episode}.py`|策略元数据与纯决策算法|
|`pve_fx/executor.py`|受控现货调用、数量裁剪、策略状态事务回调|
|`pve_fx/engine.py`、`scheduler.py`|5秒采样、热度、到达和串行执行、内存日志|
|`backend/app/services/fx/trading.py`|在现有现货内核补数量模式、bot准入及提交前回调|
|`backend/app/models/audit.py`、`services/audit_replay.py`|初始外币分配事件及现有回放适配|
|`backend/app/api/v1/admin_pve_fx.py`、`app/main.py`|超管路由、写owner下启动与停止|
|`backend/app/services/pve/engine.py`、`api/v1/admin_pve.py`|旧LMSR作用域隔离，不改其策略算法|
|`backend/scripts/{season_reset,business_reset}.py`|关闭新开关、清理绑定及分配事件|
|`thccb-frontend/src/api/pveFx.ts`、`pages/admin/pve/FxBotManage.vue`|FX类型/API和独立子页面的配置表单/账户池|
|`thccb-frontend/src/pages/admin/pve/LmsrBotManage.vue`|承接现有LMSR管理内容，继续使用旧API|
|`thccb-frontend/src/pages/admin/PveManage.vue`、`src/router/routes.ts`|父入口导航及RouterView、两个子路由，原入口默认转LMSR|

目录表中的 `pve_fx/` 均位于 `backend/app/services/`。新增必要的 `__init__.py` 随所属任务完成，不独立拆脚手架任务。

依赖顺序：1 → 2；1 → 3；1 → 4；2＋3＋4 → 5 → 6 → 7。实现中局部检查针对刚改动部分，最终集中运行相关检查，不逐任务反复跑全套。

## Task 1：数据合同、配置与旧引擎隔离

**Files:** 新 `backend/app/schemas/pve_fx.py`、`backend/app/services/pve_fx/{types,config,presets}.py`；改 `backend/app/models/bot.py`、`services/pve/engine.py`、`api/v1/admin_pve.py`、重置脚本；新增 `backend/alembic/versions/2026_10_16_fx_pve_profiles.py`。

**Interfaces:**
- `FxPairBotConfig`：设计§3.2字段，时间统一秒；`FxPveConfig` 包含 enabled 和按pair_id的已验证配置。
- `load_pair_config(db, pair_id: int) -> FxPairBotConfig`；`save_fx_config(db, config: FxPveConfig, operator_id: int) -> None`，调用方拥有事务。
- `BotProfile.market_kind/fx_pair_id/strategy_state`；默认lmsr、null、空对象。
- `types.py` 定义设计§5的 FxTickFrame、FxBotSnapshot、FxExecutionView、FxPreparedSignal、FxDecision、FxOrderIntent、FxFill；补 FxBotValues(cash/wallet/mtm/liquidation_cash)、FxExecutionResult(fill/state/rejection)。Decimal使用真实Decimal，持久JSON字符串化由状态serializer处理。

- [ ] 从当前 migration head 追加迁移；升级给原profile填lmsr，不改钱。约束LMSR无FX绑定、FX必须绑定且market_scope为空。存在FX profile时降级拒绝；没有FX profile时降级保留原User/钱包和LMSR数据。
- [ ] 将冻结参数与5户个人系数复制为生产预设，保留源快照路径及哈希说明；不把研究10000tick、冲击日程等夹具引入生产。
- [ ] 旧LMSR引擎、旧API列表/overview/修改/删除显式限定market_kind=lmsr，防止FX账户被双重调度或经旧入口改坏。
- [ ] 配置总闸默认false。season/business reset关闭新开关，清空pair配置；新增外键要求profile先于pair清理，独立FX reset也不得留下悬空profile。
- [ ] 在 `backend/tests/test_fx_pve_migration.py` 覆盖真实升级、降级和旧数据保留；复用 `test_fx_season_reset.py`／`test_admin_pve.py` 现有夹具补绑定清理和作用域案例。迁移测试不锁死“必须是最新revision”。
- [ ] 局部验证：`cd backend` 后 `pytest tests/test_fx_pve_migration.py tests/test_fx_season_reset.py tests/test_admin_pve.py -q`。仅一次性测试库；不对运行库执行upgrade/downgrade。

## Task 2：自定义初始资产、审计与FX账户生命周期

**Files:** 新 `pve_fx/accounts.py`；改 `models/audit.py`、`services/audit_replay.py`、`services/pve/service.py` 的退休注资拒绝；补reset的分配事件清理。

**Interfaces:**
- `generate_fx_bots(db, request: FxPveGenerateRequest, operator_id: int) -> list[FxPveBotItem]`：创建paused新账户，发放自定义现金/外币，无commit。
- `patch_fx_bot(db, profile_id: int, request: FxPvePatchRequest, operator_id: int) -> FxPveBotItem`：禁止改pair/基准，有轮次时禁止换模板及短线改参。
- `read_fx_bot_values(db, profile_ids: list[int]) -> dict[int, FxBotValues]`：cash、wallet、MTM、全仓卖出可得现金，列表与死亡判定使用各自明确口径。
- `fund_fx_bot(...)` 复用ledger；`retire_fx_bot(...)` 无钱包/轮次/债务时复用旧退休记账语义。创建与管理方法由API拥有事务。

- [ ] 创建schema支持pair_id、items、initial_cash、initial_foreign、naming_style；检查有限/非负/6位及现有金额上限、有效未归档交易对。每户实际初始外币记为只读基准，不经过spawn_params扰动。
- [ ] 现金按现有ledger发放；外币钱包按设计§4.1初始化，cost_basis=初始外币×发放时边际价，6位向下量化。创建前验证cost_basis乘积和外币数量均不超Numeric(16,6)，错误明确拒绝。新钱包及新profile与发放在同事务。
- [ ] 正数初始外币注册并写入 `pve_fx_initial_allocation`，在现有 audit_replay.fold_fx 增加独立钱包增量处理；零外币不写分配事件。不得伪造成交、资金池注资或价格事件。
- [ ] 完成paused/dead/retired、现金注资复活及退休前资产/债务拒绝。FX净值读数包含外币；死亡判定使用真实可卖价值。正数初始外币未卖出不能删，卖出后因已有FxTrade只能退休；零外币空白户保留原现金ledger删除语义。退休拒绝独立FxShortPosition欠币，不加入偿债算法。
- [ ] 扩展 `backend/tests/test_admin_pve_fx.py`：自定义1234现金和600外币、基准600、paused；0现金/600外币的估值；发放后池/金库/FxTrade不变；初始成本乘积越界或其他错误参数事务不留部分账户。
- [ ] 扩展已有 `test_fx_audit_replay.py`：发放后调用真实现货买卖，fold(check=True)及compare_with_live匹配。此层覆盖发放完整性，管理API不再机械复制整套回放场景。
- [ ] 局部验证：`pytest tests/test_admin_pve_fx.py tests/test_fx_audit_replay.py tests/test_credit_wp6b_pve.py -q`。

## Task 3：受控现货入口、exact-out与状态事务

**Files:** 改 `backend/app/services/fx/trading.py`、必要的 `audit_service.py`；新 `pve_fx/executor.py`；复用 `tests/test_fx_trading_service.py`、`test_fx_trading_in_session.py`、`test_fx_publication_e2e.py`，新 `tests/test_fx_pve_execution.py` 仅放bot专有行为。

**Interfaces:**
- 现有 `execute_trade_in_session` / `execute_trade` 保留所有位置参数；新增内部keyword `amount_mode='input'|'foreign_out'`、`bot_profile_id=None`、`bot_purpose='scheduled'|'admin_liquidate'`。foreign_out仅允许buy。
- `execute_trade(..., before_commit: Callable[[AsyncSession, FxTradeExecution], Awaitable[None]] | None = None)`：新成交在commit前调用，replay不重调；原HTTP不传这些参数。
- `execute_bot_order(profile_id: int, decision: FxDecision, frame: FxTickFrame) -> FxExecutionResult`：独立DB session，复用普通现货wrapper及回调，结果包含已提交fill/state或明确拒绝原因；不会自行重试未知结果。
- `liquidate_fx_bot(profile_id: int, min_out: Decimal, operator_id: int) -> FxExecutionResult`：仅超管调用，用途admin_liquidate、账户paused，完成后清轮次状态并保持paused。

- [ ] 把受控bot与真人资格分支集中到现货准入处：均检查账户启用，机器人额外检查profile与绑定、用途及状态、调度开关、无现金/外币债务；普通HTTP继续拒绝bot并执行真人TOS规则。不得从请求body接受bot上下文或伪造TOS时间。
- [ ] foreign_out在最新持锁储备上调用quote_buy_exact_out并复用现有记账。既有requested_foreign_amount记录原始外币请求量；幂等回放区分输入预算与指定输出，重复键不同模式/数量拒绝。原input模式不改变。
- [ ] 实现意图单位转换、0.5%池外币及钱包裁剪；现金不足的buy_foreign在同一资金锁内以quote_buy(可用现金)求六位外币上限，再quote_buy_exact_out执行裁剪量，不退化成预算买入；不可执行就拒绝，允许少量现金尘额。原请求量用于幂等识别，fill记录实际量和裁剪。库存buy_gold仍保持观察价乘意向量。
- [ ] 收益峰值/退出锁定是观察状态，到达后按资金锁→profile顺序先保存，包括稍后订单失败的情况；无成交不扣数量/成本。通过before_commit将on_fill成交相关状态和last_trade_at一起保存，回调失败则交易/审计/数量/成本一起回滚，但保留先前观察状态。幂等重放不重复on_fill。提交后的账户、市场增量、publisher沿用既有流程。
- [ ] 用相同初始钱包/储备，分别经真实ASGI普通现货HTTP与内部bot入口买卖，比较实际现金、钱包、成本、储备、手续费、成交和审计经济字段；忽略ID/时间/身份字段。一次参数化用例覆盖买/卖，不在其他层重复。
- [ ] 其他集中用例：HTTP仍拒绝bot；停用/paused/wrong pair拒绝scheduled；可负担的foreign_out恰好输出指定6位数量；现金裁剪后不超实际可负担量和剩余轮次量；回调故意抛错后余额/成交/剩余数量和成本不变，观察原因保留；复用已有publisher测试覆盖提交后传播，补一个bot入口样例即可。
- [ ] 局部验证：`pytest tests/test_fx_pve_execution.py tests/test_fx_trading_service.py tests/test_fx_trading_in_session.py tests/test_fx_publication_e2e.py -q`。

## Task 4：策略注册合同与冻结三策略

**Files:** 新 `pve_fx/registry.py`、`strategies/{__init__,random,inventory,episode}.py`；测试 `backend/tests/test_fx_pve_strategies.py`。

**Interfaces:**
- `FxStrategy.prepare_tick(bot, frame, rng) -> FxPreparedSignal`；`decide(bot, prepared, execution_view) -> FxDecision`；`on_fill(state, fill) -> dict`。
- `FX_STRATEGY_REGISTRY`注册 `random`、`inventory`、`episode`，返回经验证的元数据和参数schema，不执行数据库访问。
- 订单模式与状态序列化使用任务1合同；任务3只负责真实成交及提交，不复制策略公式。

- [ ] 随机：到达时N(0,120)外币意向，允许同tick多次。独立mark流不消费到达流。
- [ ] 库存：参考360个过去对数收盘均值；目标500*tanh(100*d)；相对每户真实初始基准，调整50%；买入预算语义保持。
- [ ] 短线：每tick生成个人噪声，30tick趋势/均价，趋势分母30及系数1/4；以个人sigma判入场；每轮200、退出阈值0.004/0.005/0.002/0.002。负向只卖自有外币，不借币。
- [ ] 实现整笔剩余量可平仓收益、峰值、锁定退出原因、部分成交按数量分配剩余基准；无持仓超时。填充实际成交后更新，未成交不扣数量/成本。
- [ ] 精简行为用例：600初始外币时正确计算净库存目标；29/30历史的趋势边界；正向卖出和负向买回的可执行退出收益；部分买回不超过剩余量、成本按比例、退出原因不解锁；无到达时仍预计算短线噪声。复用生产AMM数学，预期基于可观察钱包和明确现金差，不复制整套信号算法。
- [ ] 局部验证：`pytest tests/test_fx_pve_strategies.py -q`；已有研究用例只运行与轮次/执行相关的 `python -m unittest research.fx.tests.test_execution research.fx.tests.test_episodes research.fx.tests.test_entry_policies`（仓库根目录），不重跑长模拟。

## Task 5：5秒FX引擎与进程生命周期

**Files:** 新 `pve_fx/engine.py`、`scheduler.py`；改 `backend/app/main.py`、`backend/tests/conftest.py`；测试 `backend/tests/test_fx_pve_engine.py`。

**Interfaces:**
- `FxPveEngine.tick() -> dict`；`snapshot(pair_id: int | None = None) -> dict`；`get_log(profile_id: int) -> list[dict]`。
- `start_fx_pve_scheduler()` / `stop_fx_pve_scheduler()`，仅写owner下启动，5秒、max_instances=1、coalesce=True。
- 消费任务1配置、任务2账户估值、任务4纯策略、任务3executor；不直接改FxWallet/User/储备。

- [ ] 每pair独立收盘deque和z；只读过去收盘生成固定frame，短线每tick准备信号，然后独立Poisson到达、打乱并串行执行；最后采样真实最新池价更新热度。
- [ ] 每笔前重读真实钱包和执行报价；当前tick不因前序交易重新生成信号。拒绝或失败记日志后等待以后正常到达，不添加订单补偿/重发队列。
- [ ] 库存/短线同tick仅一次成功成交，随机可多次；消费或暂停不被当作永久死亡，死亡使用现金＋FX全仓可得现金。
- [ ] 重启保留strategy_state，重建窗口和z；不读取10秒K线假充5秒样本、不补跑漏tick。只需保存策略必要字段，不持久化全部随机流/热度。
- [ ] main在FX行情runtime就绪、写owner明确后启动；反序停机并等待进行中的交易。测试conftest禁用新调度别名，避免后台真实tick污染测试库。
- [ ] 小型确定性到达用例调用真实策略/AMM/执行器验证：未到达仍预计算、事件前信号固定、同tick成交次数限制、tick末收益影响下一tick热度；一个真实DB重建engine案例验证未完成轮次继续退出。不要复制整个研究模拟器搭新测试框架。
- [ ] 局部验证：`pytest tests/test_fx_pve_engine.py tests/test_pve_engine.py -q`。

## Task 6：管理API与可配置页面

**Files:** 新 `backend/app/api/v1/admin_pve_fx.py`、`thccb-frontend/src/api/pveFx.ts`、`thccb-frontend/src/pages/admin/pve/FxBotManage.vue`、`thccb-frontend/src/pages/admin/pve/LmsrBotManage.vue`；改 `app/main.py`路由注册、`pages/admin/PveManage.vue`、`thccb-frontend/src/router/routes.ts`；测试复用 `test_admin_pve_fx.py`。

**Interfaces:** 设计§8列出的管理API与请求形状；FX API前缀 `/api/v1/admin/pve/fx`。前端父路由 `admin-pve`、子路由 `admin-pve-lmsr` 与 `admin-pve-fx`；路径分别 `/admin/pve/lmsr`、`/admin/pve/fx`。FX子页面只消费pveFx API，LMSR继续原API；共享排行榜开关继续原config API。

- [ ] 实现overview/config/list/generate/patch/batch/fund/liquidate/delete/log，全部current_superuser；账户路径验证market_kind及目标归属。批量操作与单户使用同一参数校验，提交前列出目标账户数量。
- [ ] overview返回冻结预设及策略字段schema；创建默认paused，表单可改现金、外币、人数，展示预计总发放；不出现写死的1000库存基准。
- [ ] 按现有FxManage父子页面模式，将原PveManage内容移至LmsrBotManage，PveManage仅保留导航和RouterView；注册两个子路由，`/admin/pve`默认跳转LMSR。各自仅加载本页面数据，保留父入口书签与现有侧栏路径高亮；不为简单页面拆分新增永久测试。
- [ ] FX子页面按交易对看账户、余额、MTM/清算值、轮次、日志；提供公共参数与个人系数编辑、批量启停、暂停后清仓及退休。没有轮次时才允许换短线模板/参数，失败原因在界面直接显示。
- [ ] 复用已有“排行榜是否包含机器人”“财富统计是否包含机器人”开关，提示影响全部bot；不重复保存FX版开关、不自动关榜。
- [ ] API用例只补超管权限、批量参数实际生效、paused清仓后资产/状态和可退休；资金与策略完整性留给任务2/3/4，前端不机械新增表单测试。
- [ ] 验证：`pytest tests/test_admin_pve_fx.py -q`；`cd thccb-frontend` 后 `npm run type-check`、`npm run build`。可最多截图核对页面，不启动浏览器自动点击、填表或交互验收。

## Task 7：集中验收、操作说明与交付

**Files:** 更新 `docs/pve.md`；按实际修改补 `test_pve_filters.py`、reset与审计已有用例；设计/计划中标记完成项和实际结果。

- [ ] 自查数据到UI的任务覆盖，核对生产未引用research；6位金额、初始库存基准、MTM与清算值标签、停用/暂停、退出状态回滚、清仓退休都符合设计。
- [ ] 复用已有过滤测试加入一个持FX钱包的bot：隐藏时人类名次连续，bot不出现；公开成交与池价格不因隐藏被删。仅覆盖现有过滤与FX资产组合，不新增排行榜机制。
- [ ] 一次性集中运行：`cd backend` 后 `pytest tests/test_fx_pve_migration.py tests/test_admin_pve_fx.py tests/test_fx_pve_execution.py tests/test_fx_pve_strategies.py tests/test_fx_pve_engine.py tests/test_pve_filters.py tests/test_admin_pve.py tests/test_pve_engine.py tests/test_credit_wp6b_pve.py tests/test_fx_trading_service.py tests/test_fx_trading_in_session.py tests/test_fx_audit_replay.py tests/test_fx_publication_e2e.py tests/test_fx_season_reset.py -q`。已有business reset相关用例按实际文件名追加，限新增开关和数据清理影响，不全库扩测。
- [ ] 最终新改动涉及借贷/通用现货分支时，仅追加已有 `test_fx_net_trading.py` 和 `test_fx_borrow_buy.py` 回归，检查新增默认参数没有改变原调用；不增加对应功能。
- [ ] 文档写明：创建时自定义资产、默认paused、按pair开闸、短线只到达时退出、重启热度/窗口预热、真实费用、隐藏排名的全局范围，以及暂停→清仓→退休流程。
- [ ] 按实际变更做一次独立代码审阅，区分核心缺陷与低概率可人工处理问题；不为理论风险新增机制或反复扩大审阅。
- [ ] 交付代码、迁移文件与检查结果，列明未验证项。默认所有开关仍关闭；不运行生产迁移、不创建真实bot、不实际发放资产、不部署。用户另行授权上线后再执行。

## 验证环境与交接约定

- 上述pytest均由 `backend/tests/conftest.py` 的一次性SQLite配置或现有fx fixture隔离；执行前检查DATABASE_URL目标，只能使用测试库。sandbox内不能写`/dev/shm`时，显式使用`/tmp`一次性文件，不切换运行库。
- Python/pytest使用仓库当前可用环境；不为本计划安装依赖或启动新数据库。PG专项检查仅在已有可用一次性测试库且实际变更要求时运行，不新增CI/E2E环境。
- 文档阶段不要求运行功能测试。实施阶段各任务局部检查完成后，在完整改动上执行一次相关集中检查；只有新改动或具体失败才追加。
- 执行前检查工作树，保留用户已有research和文档改动；需要隔离时遵循using-git-worktrees。只提交本计划的实现文件，不将既有输出或研究目录整体纳入提交。
- 任务提交按独立可审阅交付物组织，不为每个机械小步骤强制提交。当前只准备文档，不自动提交或推送。
