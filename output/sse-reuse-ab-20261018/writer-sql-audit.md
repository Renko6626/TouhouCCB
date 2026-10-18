# Writer 风控到买入的只读 SQL 审计

审计基线：工作树 HEAD `58101e1fc78846bb5c1dc0919374e4ba76753753`，后端 profile 基线 `6c7675722cda265bd205dced6ce0f939ae7ac769`。本报告涉及的 `fx/{writer_ops,net_trading,trading,shorts}.py`、`credit/risk.py`、`loan_service.py`、`market_locks.py`、`site_config.py`、`ledger_service.py` 九个文件 SHA256 均与 profile 的 `source-hashes.json` 相符。以下源码路径均相对项目根，行号指该基线。

本次只读取源码和既有 profile；唯一新增文件为本报告。未改产品或测试，未启动 PostgreSQL、HTTP 服务、浏览器或负载，未执行测试或提交。

## 结论与建议顺序

建议先讨论并批准一个很小的实现范围：**在同一个 writer 事务、同一个 User 锁之后，把已经加载的 User 和全部 short rows 传给账本发现逻辑复用**。NetTrade 有完整证据支持两次重复读取：`lock_user` 后又刷新同一 User，`_lock_short_rows` 后又列读同一批有债务的 short rows。普通现金 NetTrade 已经不做完整账本发现，收益主要落在有黄金债务、融资或外币债务的路径。

旧 BorrowBuy 还有一项独立局部机会：已有 User 锁后调用现成的 `increase_debt_locked`，替代再次锁 User 的 `increase_debt`。NetTrade 已经使用 locked 版本，不能重复计算收益。本轮 profile 没有旧 BorrowBuy command 的样本。

不要以“查询很多”为理由删全组合持仓读取、User/Pair/资金行锁、幂等、利息结算或资金/审计/历史写入。现有 flush 多数服务于真实的主键依赖或资金快照，尚没有可以直接删除且证明等价的一组 flush。

## 证据与计时口径

证据根：`/data/sunyunbo/www/TouhouCCB/output/fx-writer-profile-20261018/`。

- `report.md`、`analysis.json`：stress 成功 NetTrade 215 次，handler 平均 127.61ms，SQL cursor elapsed 平均 69.12ms，SQL 平均 17.60 条；discover 70 次平均 21.22ms，simulate 216 次平均 0.20ms。active 对应 handler 79.86ms、SQL cursor 37.44ms、17.54 条 SQL。无 profiler 上轮约 88ms 仅作扰动背景，不能当本轮优化对照。
- `profile/profile-commands.json`：stress 成功 NetTrade SQL 条数分布为 `15×146、20×18、23×25、25×22、27×4`；另一个业务拒绝为 11 条。active 成功分布为 `15×205、20×22、22×1、23×30、25×31、27×7`。这些记录没有请求 side/borrow 参数，不能把每个条数组无条件命名为一种业务路径。
- `profile/profile-events.json`：stress writer 内 discover：NetTrade 70 次、ShortCover 21 次；NetTrade 的 `increase_debt_locked` 25 次。函数成功不等于整笔成交成功。
- `profile/profile-sql.json` 是按 phase/route/span/statement 的**聚合记录**，没有逐笔 SQL 时间线、参数或数据库计划。本报告下文的 `[n]` 是其从 0 开始的 JSON 数组下标，便于精确复核。顺序来自实际执行源码，raw 证明相应 SQL 确实执行及其聚合次数；同一次 flush 内的 ORM 写入列为一组，不伪造每笔完整 SQL trace。

SQL cursor elapsed 包含 PostgreSQL 执行、可能锁等待、传输和事件循环调度；handler/span 也含 await。它们均不是 DB 纯执行或 Python CPU。report 中 PG 累计 server execution 也不能当 CPU 或每笔串行关键路径。此审计不把节省查询数换算成吞吐或延迟承诺。

## 一、主路径 NetTrade 的真实 SQL 阶段

入口 `backend/app/services/fx/writer_ops.py:149–173` 创建 writer 自己的 session，调用 in-session kernel，最终 flush 后 commit；commit 成功后先 install snapshot，再发账户及行情提示。NetTrade 的 `credit_deps` 实际从 `None` 开始，不能拿 producer/query 阶段数据授权。

核心在 `backend/app/services/fx/net_trading.py:258–366`。成功请求按以下阶段串行执行：

|顺序|读取或写入|用途、锁与证据|
|---|---|---|
|1|`SELECT fx_pair ... FOR UPDATE`|目标市场当前储备/状态/费率和互斥，272 行；`trading.py:55–61,182–192` 带 populate_existing。raw `[116]`，stress 216 次。|
|2|`SELECT user ... FOR UPDATE`|资金、债务、权限、版本；273 行；`market_locks.py:38–46` 强制刷新 identity map。raw `[117]`，216 次。|
|3|一次 receipt + legacy trade EXISTS 查询|274 行；`net_trading.py:232–247`，请求 key 同时检查 `fx_execution` 和 `fx_trade` 两个命名空间。先回放/比对参数，再在 283 行检查 expected economic version。raw `[118]`，216 次。|
|4|`SELECT fx_wallet ... FOR UPDATE`|285 行，只读目标钱包，`create=False`。需要完整 wallet 的 amount/basis；raw `[119]`，216 次。|
|5|`SELECT fx_short_position ... ORDER BY pair_id FOR UPDATE`|286 行，锁本人所有 short 行，包括已归零行，为分类、全体结息及写账提供 ORM 对象。raw `[120]`，216 次。|
|6（条件）|完整账本发现的 5 个 SELECT|287–291 行：融资/auto-borrow、现有黄金债务、任意外币债务、卖出超过钱包时执行，详见下一节。普通无债现金买入不走。|
|7|`SELECT fx_treasury ... FOR UPDATE`|292 行，真实库存及手续费收入；raw `[121]`，216 次。|
|8|计算统一时点 T、模拟和必要风控|293–303 行；`_simulate` 使用已加载 user/wallet/positions/treasury 和 deps。热配置命中缓存时没有 SQL；风控不再重读账本。|
|9（条件）|旧债利息在 T 结算，暂存 User/short 修改及审计|309–311 行调用 `shorts.py:523–571`，这一步自身无显式 SQL/flush。不能从低利息样本推导永久无写入。|
|10|`INSERT fx_execution ... RETURNING id`，及此前待 flush 的利息更新/审计|313–317 行首次显式 flush，用 execution.id 关联各腿。raw `[122]` 215 次；利息更新/审计可见 `[183]`、`[184]`。|
|11（融资腿）|借款资金/版本 UPDATE + `INSERT ledger_entry RETURNING id`|350–353 行调用 `increase_debt_locked`，内部 `ledger_service.record_entry` 在 `ledger_service.py:62` flush，并以 ledger id 添加借款 audit。raw `[179]`、`[180]` 各25次。|
|12（每个现货腿）|User、Pair、Wallet、Treasury 更新/插入 + `INSERT fx_trade RETURNING id`，以及先前待写 audit/history|354–358 行调用 `trading.py:532–599`；580 行 flush 后才构造该 trade 的 pool event 和 audit。raw `[123–125]`、`[127–128]`；新钱包 INSERT `[206]`。NetTrade 已把 treasury 传入，此处没有第二次 treasury 查询。|
|13|更新 receipt.response_payload；最终 flush 审计/history/receipt|362–366 行和 `writer_ops.py:159`。raw `[129–131]`。随后一次 commit，直到成功才释放相关行锁并 install/publish。|

热缓存、已有 treasury、单现货腿、无旧债及利息写入时，可由以上阶段还原 **6 SELECT + 1 receipt INSERT + 5 笔资金/成交写入 + 3 笔审计/history/receipt 写入 = 15 SQL**，与 raw 中大量 15 条命令吻合。若钱包不存在，NetTrade 等到现货腿才创建，INSERT 替代 UPDATE。配置冷缓存、利息、还款、跨零多腿、ORM 批处理会改变数量。不能把 17.60 平均值当每笔固定路径。

### 1.1 discover 的五个 SELECT，哪些已有

`backend/app/services/credit/risk.py:503–544`：

|顺序|SQL / 数据|已有程度与判断|
|---|---|---|
|D1|`SELECT user ... WHERE id=?`，populate_existing，514 行 / helper 323–338 行|NetTrade 的步骤2已获取同一行、同一事务内持续持锁；到步骤6还没有资金修改。**已证实重复读取**。raw `[174]` stress writer 91 次，370.07ms 聚合 cursor elapsed，包含其他 ShortCover 路径。|
|D2|`SELECT Position.outcome_id, amount, Outcome.market_id ... amount>0`，515–518 行|目标 FX wallet/short rows 无法提供 LMSR 抵押资产；**必须继续从数据库获取完整集合**。raw `[175]` 90 次。这里只读 Outcome 的 market_id 关联，不从 SQL 读跨市场行情。|
|D3|`SELECT FxWallet.pair_id, foreign_amount ... foreign_amount>0`，522–524 行|步骤4只有目标 wallet，没有本人其他 pair 正持仓。目标数量重叠，但**全组合查询整体不可删**。raw `[176]` 90 次。|
|D4|`SELECT short pair_id, principal, interest, accrued_at ... principal>0 OR interest>0`，527–532 行|步骤5已有该用户**全部** short ORM 行，同样包含这些字段，且有 User 锁。可从同一集合按同样谓词构造持久 short snapshot。**已证实重复读取**。raw `[177]` 90 次，363.53ms 聚合 cursor elapsed。|
|D5|`SELECT SiteConfig.key,value ... IN (loan_daily_rate,sell_fee_rate,credit_new_risk_frozen)`，541 行及707–719 行|这是直接列读，绕开 TTL/identity map；还会更新热冻结 flag。**必须保留这次当前规则读取**。raw `[178]` 90 次。|

D1 比后面多一条，是观测窗口/在途 span 边界的可能结果；原始数据不足以唯一归因，不当作产品异常。完成以上读取后，543 行同步捕获所需已发布市场快照，只捕获一次；缺快照不会 SQL 补价，也不会重捕获（498–500 行）。

`check_new_risk` 的 `_prepare_post` 在 `deps.account_locked=True` 时直接使用传入数据（722–726 行），因此不能再声称最终检查又做一遍相同5条 SQL。其经济版本、冻结、全组合估值及空头回补成本逻辑仍须保持（906–969 行）。

### 1.2 几种账户状态如何分流

- **纯现金无债买入**：仍锁 User、目标 wallet、所有 short rows、treasury，才能确认实际资金和单方向状态；已跳过 discover。已有其他正资产本身不触发完整风险发现。此路径不能从 D1/D4 复用获益。
- **已有黄金债务 + 买入**：需要 D2/D3 的全组合正资产，以及任何外币义务；T 用于模拟含息债务，交易前统一结息。仅复用目标钱包不足以维持保证金语义。
- **显式融资买入**：先完整模拟借款后的现金/债务/持仓并风控；所有旧利息只计到 T，新本金只在新多头腿入账，从 T 开始计息。借款 ledger 和成交在一次 writer commit 中。`increase_debt_locked` 与 spot 都会 bump economic version，不能在优化中合并为一次 bump。
- **已有其他 pair 外币债务**：即使黄金债务为0也必须发现/计入 K。D4 可复用全部锁后 short rows，不能只看目标 short 或 `User.debt`。
- **同 pair 空头买入跨零**：`_simulate:99–138` 先用既有现金回补、预算余量才建多头，借款只给后者；执行 `319–358` 按腿顺序写账。`_write_cover_ledger` 的 flush（`shorts.py:693`）拿 trade id，再产生对应 pool event/audit；第二腿后续 flush 可写入第一腿事件。需保留先息后本、proceeds basis、版本逐腿推进及第二腿失败全回滚。
- **同 pair 同时正钱包与外币债务**：`_simulate:48–49` 拒绝，保留人工复核语义。不可用“只读取正债务”优化掩盖此状态。

## 二、旧 SpotTrade / BorrowBuy 的路径与额外重读

`writer_ops.py:98–122` 分派到 `trading.execute_trade_in_session`。核心 `backend/app/services/fx/trading.py:368–529`：

1. Pair FOR UPDATE → User FOR UPDATE → `reject_net_key` 查询 `fx_execution.id`。
2. 买入用一个 aggregate-short-state + outer join FxTrade 的 SQL，同时读取“本人是否有外币债务”和旧幂等成交（374–387 行）；回放在进一步风控之前完成（402–410 行）。这不是 NetTrade 的 `_read_net_request_identity`，也不是仅查成交的重复查询，须保留两个 key namespace 的语义。
3. 融资检查 loan_enabled；`financed or user.debt>0 or has_short_debt` 时执行 discover 五读（412–418 行）。这里同样可复用已锁 User，**但尚未有全部锁后 short ORM 行可供直接复用 D4**。不可把一个 has_debt 布尔值当完整外币义务清单。
4. fx_enabled、Pair 状态/TOS/同 pair short 限制 → 报价及现金核对 → 目标 wallet FOR UPDATE；不存在时 `_wallet_lock:268–271` 会立刻 INSERT + flush。`available_cash` 虽被调用两次（450、481 行），当前只是读取并验证 `user.cash`（`credit/cash.py:17–22`），**不是两条 SQL**。
5. T=`trade_now`（484 行）；有债/融资时做交易后风险检查。融资先计算旧含息债务 + 新本金，以 T 作为 risk_deps 的 debt clock，避免新本金补算历史息；普通有债买入结息并保留必要 audit（485–525 行）。
6. **旧融资仍调用 `loan_service.increase_debt`（514–516 行）**。该函数 `loan_service.py:121–126` 再次 SELECT User FOR UPDATE + populate_existing，随后委托同一个 `increase_debt_locked`。这里已有同一 User 锁，没有其他会话可插入该账户写入；可用现成 locked 入口省这一次 SELECT，保持 now/rate/source/reason/grant_cash 全部参数。NetTrade 在350–353行已这样做。
7. `_write_spot_ledger` 先改 User/Pair/Wallet，再在缺 treasury 参数时 SELECT Treasury FOR UPDATE + populate_existing（557–559 行）。ORM autoflush 会在查询前写入尚未 flush 的 User/Pair/Wallet/借款 audit。这个 SELECT 是旧现货路径的**第一次 treasury 读取**，不能当作 NetTrade 已读 treasury 的重复。其后改 treasury、插入成交，580行 flush，追加 pool event/audit。
8. SpotTrade wrapper 会 `refresh(trade)` 规范化返回持久 Numeric 精度（`writer_ops.py:105–109`），可能先 autoflush 剩余事件；BorrowBuy 使用专门 response，不走该 refresh。最终仍由 writer flush + commit，commit 后 install/publish。

本轮 active/stress raw writer command 只有 NetTrade、ShortCover，旧接口以上结论由源码确认，**没有该路径现场 SQL 数量/耗时证明**。SpotTrade 的 refresh 注释明确关联响应精度，先保留；要移除需独立证明首次响应与回放精度一致。

## 三、建议的最小方案及边界

此处是可供批准的方案，尚未实现、尚未验证等价。

### A. 优先：同事务已锁对象复用

目标文件：`credit/risk.py`、`fx/net_trading.py`；旧融资的独立小项在 `fx/trading.py`。

建议按以下最小变更排序，**首批只做 A1+A2**；二者在同一锁后发现位置，可一起实现并集中验证。A3是旧接口的独立机会，不要求随本批一起完成。B与flush调整也不与首批捆绑。

|顺序|最小变化|直接收益|需要的具体验证|
|---|---|---|---|
|A1|NetTrade锁后的User交给发现逻辑，保留其他调用者刷新|每次NetTrade discover省1读|持锁前session旧User被锁读刷新；持锁后的cash/debt/freeze/version进入风险决策，回放及版本拒绝保持。|
|A2|NetTrade已锁全部short rows交给发现逻辑|每次NetTrade discover再省1读|其他pair债务也参与K；只剩利息也算义务；零行不参与义务；同T结息/新本金时钟、跨零及第二腿失败资金/receipt/history全回滚。|
|A3（独立可选）|旧BorrowBuy用现成increase_debt_locked|每次旧融资省1次User重锁|同T借款、loan ledger/audit、版本、成交和库存同事务；失败不留下借款或成交。|

- 保留通用 `discover_dependencies(session, user_id, ...)` 的锁外默认行为与完整刷新语义，避免影响 quote、admin、跨产品调用者。通过显式的内部锁后入口/可选输入只给已确认持锁的 writer 调用点传入 **User ORM 对象和全部 short ORM 行**。最终接口形状需随方案批准，避免仅靠一个可误用的 `account_locked=True` 静默跳读。
- NetTrade 在原来的 discover 位置使用步骤2/5的对象；D2、D3、D5仍执行 SQL，随后仍在同一位置捕获一次已发布价格。筛选 short debt 保持 `principal>0 OR interest>0`，使用持久 principal/interest/clock，不提前结息，不只读目标 pair。空列表必须是已知无行，不能误当“未提供输入”再查询。
- 旧 Spot/BorrowBuy 可同样复用已锁 User；其 D4 仍保留查询。旧融资改用现有 `increase_debt_locked`，继续传同一个 T，不改 borrow/trade 的事务、ledger、audit 或版本行为。
- 复用期间不新增缓存、session.info 状态、版本协议、队列、数据库字段或跨请求账户镜像。调用者必须同 session、同 user id、持锁直到 commit；就地组装到当前 DependencySet 即可，禁止复用上笔命令的对象。

**SQL 收益上限**：NetTrade 每次实际需要 discover 最多省2条 SELECT，普通无债现金单最多省0条。按 stress 215个成功中69个非15条操作、加1个拒绝且discover共70次的源路径核对，成功命令理论减少约138条，即平均约0.64条，17.60约降至16.95（约3.6% SQL数）；这是当前样本和路径的估计，不是性能实测。raw 的 D1+D4 合计733.60ms还包含 ShortCover 与窗口边界，不可全归 NetTrade，更不能直接从127.61ms中逐笔扣除。其余 elapsed 仍受事件循环、ORM处理、其他SQL与提交影响。

旧融资每笔可独立省1次 User 重锁；再复用发现中的User时总计最多2次，但没有本轮样本支持全局收益比例。若将此优化扩到独立 ShortCover，需要另行逐调用点评审；本报告不自动扩大实施范围。

**主要风险与约束**：把未锁/别的session/旧对象误传成已锁输入，会绕过 `_refresh_user_authority` 防止的真实旧 cash/freeze 问题。故保留默认刷新，并仅在明确的当前执行路径使用复用入口。这里 User 锁后到 discover 之前目前没有待写资金状态；不能把此事实推广到所有调用者。`populate_existing` 和 autoflush 对未复用读取保持原状，不能用全局 no_autoflush 或关闭刷新换性能。

### B. 可后续讨论：把两次正持仓列读合为一次 UNION ALL

D2、D3都不可省略其数据，但可以在数据库中按统一列形状一次返回 product/group id/asset id/amount，把2个 round trip改成1个，仍读取锁后的全组合 LMSR正持仓与FX正钱包。这不需要跨请求状态。每次discover理论再省1条，风险主要是组键/资产id、Decimal精度、缺关联和零持仓筛选的等价性。相较 A，它改变查询及组装方式，收益更小，建议先不纳入首批；不能引入跨表笛卡尔JOIN或用目标wallet代替全部钱包。未跑EXPLAIN，不能保证 server执行更快。

### C. 涉及架构/语义变化，须单独批准，不纳入最小方案

- 跨命令缓存本人账本、利率/冻结规则镜像，或把 writer 内存持仓当资金权威：需要新的失效、同步或恢复机制，并改变现有账户边界。
- 用 Pair内存镜像直接删目标 Pair锁/查询，或只凭同市场唯一writer删 User/Wallet/Short/Treasury锁：跨市场同账户写入及管理路径仍存在，超出已确认路线。
- 把 D5 改为 TTL/启动缓存：改变当前冻结生效、维护期规则读取语义；现有 site_config TTL缓存也不能替代 `_current_rates` 的直接列读。
- 取消 receipt INSERT/UPDATE、合并资金/audit/history为异步落库、预分配id/改主外键关系以减少flush：涉及模型/事务边界/接口设计。不能为“少一次flush”默认为已经授权。
- 去掉完整 short 行锁后只做存在性查询、按请求分支延后锁：可能减少纯现金读量，但引入分支锁顺序变化，当前无必要性/等价性证据，不建议首批实施。

## 四、必须保留项与常见误判

1. Pair→User→本人资金行→Treasury 的既有顺序、User锁覆盖到commit、锁后 fresh own ledger，执行授权不能来自入队前query/quote或外层传入的旧deps。
2. 所有仍执行的锁读/权威刷新保留 populate_existing；autoflush保障同事务待写状态不会被强制刷新覆盖。复用对象仅在调用者明确持锁且对象最新的当前点成立。
3. 完整本人LMSR/FX正持仓、全部外币义务、统一T、全组合风险、冻结/版本检查；`_prepare_post`锁后快路已经存在，最终检查无须再消SQL。
4. loan+trade同一次commit；新借款不为先发生的cover腿供资，不对新本金倒算历史息；dust-only clock规则、跨零两腿和错误全回滚。
5. 两种幂等命名空间、请求参数一致性、回放先于版本/新风险拒绝；receipt与trade引用及结果缓存不能移除。
6. 资金/债务/钱包/库存/手续费储备、economic_version与pool_version、借款ledger、每腿trade/audit/pool history都保留。raw中Treasury只更新updated_at并不能证明“没用”，样本费率/量化不能替代真实手续费逻辑。
7. 需要receipt.id、ledger.id、trade.id构造后续引用的flush先保留。flush本身不是另一笔commit，也不必产生SQL；SQL计数已包括flush内语句，不能把flush elapsed与SQL cursor elapsed相加。重复结息函数在同T的计算也不等于重复SQL，不作为首批SQL优化。
8. commit后install再发布、失败回滚/未知结果语义与历史完整性保持；不能通过删行情历史来换writer速度。

### Flush逐处判断

|位置|现有目的|本批判断|
|---|---|---|
|`net_trading.py:317` receipt flush|拿execution.id；同时落此前利息和audit|保留，后续各腿用真实execution id。|
|`ledger_service.py:62` loan flush|拿ledger.id作借款audit.ref_id；资金快照来自已变动User|保留，不能把借款ledger、audit与成交拆事务。|
|`trading.py:580` spot flush|拿trade.id/created_at供pool history与audit；也写入前腿待写事件|保留，当前调用者马上使用这些值。|
|`shorts.py:693` cover flush|拿cover trade.id，随后追加该腿pool history/audit|保留，跨零第二腿失败仍由外层全回滚。|
|`shorts.py:502` open flush|同样拿short-open trade.id再写history/audit|公共多腿路径保留，不扩成本批改动。|
|`writer_ops.py:159` 最终flush|落剩余audit/history/receipt后，capture changes/accounts，再commit|保留；不等同额外commit，不一定每次都有SQL。|
|旧`trading.py:271` 新wallet flush|新钱包行立刻持久化；后续权威查询可能autoflush/刷新|不纳入首批；NetTrade本来就延迟到leg中创建，旧接口要单独论证。|
|旧`trading.py:558` Treasury查询前autoflush|防止populate_existing覆盖本事务待写状态；先写User/Pair/Wallet等|保留默认autoflush；删除该查询并无“重复treasury”证据。|
|旧`writer_ops.py:108` refresh trade前autoflush|准备持久精度响应，同时可能写入待写history/audit|独立于NetTrade首批；保留首次响应/回放精度。|

以上flush有一部分只是在同一事务中提前写入已有变化，并非业务需要每个阶段提交。首批只移除两次读，不改这些flush，便于把账户复用和写入次序的影响分别评审。

## 五、实施后的合理验证范围（本次未执行）

先复用已有测试，按首批实际改动选取，集中运行一次，不新增整套验证项目：

- `backend/tests/test_fx_net_trading.py`：已有跨零买入、融资只供新多头、第二腿失败回滚、统一利息T与audit回放、key namespace、版本变化不误开仓的真实行为用例。
- `backend/tests/test_fx_borrow_buy.py`：仅在改旧融资调用时运行，覆盖借款/成交成功回放与拒绝不变更。
- `backend/tests/test_credit_risk.py`：完整抵押集合、fresh User/cash/freeze、旧deps rebase等现有用例；新复用入口必须保持通用未持锁入口这些保障。
- 若改 short数据输入，选用 `test_fx_short_trading.py` 中旧债T与dust clock、黄金债结息等相关用例即可，不自动扩大到全强平系统。
- 只有需要证明持锁并发约束时，使用已有 `backend/tests/pg/test_pg_credit_admission_contention.py` 的双市场同账户/旧预取场景；需依赖可用隔离PG环境，另行按已授权验证范围执行。本审计未启动PG。

对于 A 新入口，已有用例若没有覆盖“同session之前加载旧User，持锁后刷新，再传入复用”和“全部short列表含其他pair、零行/仅利息行”的真实风险，可在最合适层补最小行为覆盖；不新增源码字符串断言或把固定SQL总数当永久不变量。

性能复核先用短的、可观察SQL顺序/数量对照，确认目标路径少2读、旧融资少1重锁，其他查询/落库仍在；本轮既有聚合不足以证明新实现。只有用户后续批准时再做同条件无profile A/B，且与SSE改动分开归因。不能以单元测试通过宣称吞吐或延迟提高。
