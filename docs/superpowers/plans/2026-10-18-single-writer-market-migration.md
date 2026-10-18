# 每市场唯一 writer 与已提交快照迁移方案

> **状态：用户已确认，2026-10-18 开始隔离分支实施。** 后续执行使用 `superpowers:subagent-driven-development`，沿用用户指定的 sol 子代理；逐项交付、审阅。项目 AGENTS.md 的范围与测试约定优先于技能模板。

**Goal：** 消除普通行情、账户附带行情和跨市场估值对交易队列的等待，同时让同市场经济写入严格按唯一 writer 的顺序提交并发布。

**Architecture：** 复用 LMSR 已有 writer 和不可变 PricingRegistry；为 FX 增加每 pair 唯一的轻量 writer。首轮 FX 成交计算继续使用事务内数据库真实状态和行锁，只迁移调度、提交归属与快照发布，不同时把金融计算改为内存权威。

**Tech Stack：** 现有 Python/asyncio、SQLAlchemy AsyncSession、PostgreSQL、不可变 market_read_state；无新依赖、表、持久化任务系统。

**Spec：** 用户本轮关于唯一 writer、独立已提交快照、执行时跨市场风控及账户事务的要求，具体口径在下文固定。

## 1. 已审计现状及迁移边界

代码核查以当前主工作区为准：核心市场读取与 LMSR writer 与先前迁移提交 `330f4e6` 一致；主工作区存在其他未提交修改，不能整目录覆盖。早先局部去 GATE 草稿保留于 `perf/request-snapshot-risk-20261018` 的 `6526700`，未并入当前主工作区。它可提供读取与风险调整的参考，不能当作本方案已完成。

| GATE 现在承担的职责 | 替代方式 |
|---|---|
| 同市场交易、注撤资、管理互斥 | 同一市场唯一 writer；数据库资金事务和行锁继续保留 |
| 借款期间冻结其他抵押市场 | 删除冻结；执行检查时复制其他市场已发布快照 |
| 普通账户/行情读等待写入与发布 | 删除等待；账户与行情分别保证其自身完整性 |
| 冷加载、重载与成交发布互斥 | 活跃市场仅 writer/bootstrap 发布；重载入队；普通冷读不安装 |
| 账户通知与价格投影之间的覆盖屏障 | writer 提交并安装快照后再发通知；读取协议明确不保证跨对象原子截面 |
| 写实例所有权 | 保留现有 OWNERSHIP，它独立于 GATE，不由进程内队列替代 |

LMSR 买卖、关盘、恢复、结算、强平、PvE 实盘已走每市场 consumer。缺口是已有市场 `register_market/reload_state` 绕过 consumer，以及 op 内账户通知早于快照安装。SQLAdmin 的经济模型 CRUD 在 main 中已禁用，不把它误算为待新增写入口。

FX 的无提交计算内核已有现货、净指令、开空/平空、强平卖出/平空；但 commit 分散于多层 wrapper、条件单、强平、注撤资和管理入口。不能仅删除 GATE 或仅在 HTTP 交易入口加队列。

## 2. 推荐的精确语义

### 2.1 一笔交易的时序

```text
请求认证/解析 → 结束请求侧数据库事务与连接占用 → enqueue
  → 本市场 writer 取出命令
  → 新事务、锁定本市场及本人账户、检查所有权/账户版本
  → 复制其他市场最近已发布行情，进行风控
  → 用本市场真实执行状态检查滑点并执行金融内核
  → 同事务写余额/债务/持仓/流水/订单状态/审计
  → flush 并构造最终不可变快照 → DB commit
  → 同步安装完整快照 → 账户/行情通知 → 返回结果
  → 才开始下一条命令
```

保证的是**成功进入队列的顺序**，不是浏览器点击时间或网络到达时间。不同市场可同时运行；同一用户跨市场操作仍可能等待 User 行锁。队列不会消除数据库提交耗时、账户争用或计算耗时。

“执行检查时”推荐固定为 **writer 出队、锁定账户后、资金变更前**。这时复制抵押市场的已发布快照；后续不因其他市场变价撤回该笔操作。目标市场执行价格和滑点仍按自己 writer 当前状态检查。入队前的预览不构成借款额度承诺；若要在入队时保证额度，则需要预留资金/额度机制，超出本次范围。

借款买入、多腿净交易必须作为一个市场命令执行，仍是一笔资金事务，不能拆成先借款提交、再排队成交。

### 2.2 普通读取

writer 执行或排队期间，普通行情直接返回最后一次完整发布的不可变 record。DB 已提交、快照尚未发布的小窗口允许读到上一份完整快照。正常响应的 as_of/stamp 描述所附快照，不冒充“数据库此刻的最新值”。

活跃 owner 市场启动时先加载，再公开可接单状态。普通读取没有发布权限：冷 SQL 结果仅用于当前响应，不能 install、invalidate、复活已删除市场，不能成为风控 writer 的可用状态。冷查询结束时若已有健康的新发布 record，优先使用该 record；若已删除或不可用，不能用迟到 SQL 结果反转该状态。无需另建 generation/tombstone 系统，利用 actor 生命周期和现有状态完成判断。

活跃市场缺失健康发布状态时，经济写暂拒并走 writer 的现有恢复路径；普通活跃行情返回暂不可用。终态市场、非 owner/只读实例允许响应级 SQL fallback，不获得发布权限，也不承诺复用本进程 owner 内存。保持当前单经济写实例部署，不引入跨进程缓存同步。

报价只表示观察到的行情，最终成交仍重验。LMSR quote 从 REGISTRY 取计算输入，静态名称等元数据可继续查库；列表、详情、排行榜、历史不强行改造成全部内存化。

### 2.3 跨市场借款与风控

只锁本人账户及本次实际写入的目标市场，不锁其他抵押市场，不等待其 writer，不向其 writer 发送查询命令。一次检查复制所需已发布 record，后续计算复用这组输入；不同市场允许各自有不同的观察时间，不承诺全市场共同原子时刻。

本人余额、债务、持仓、冻结状态、economic_version、依赖市场集合仍在账户锁和事务内重验。目标市场交易后的模拟状态仍由目标 writer 提供。独立借款、还款、兑换、管理现金操作若不改变 AMM，不必进入任一市场队列；保留其账户事务并复用执行时行情快照。

**追加要求：同一账户跨市场串行使用保证金。** 允许同一个人的两条请求分别进入两个市场队列，但两者不能同时用同一份旧账户状态通过授信。每个执行 handler 在账户相关风控之前取得同一 User 行的排他锁，取得后重新读取本人余额、全部相关债务、持仓和经济版本，直到本次提交/回滚才释放。入队前发现的依赖和风险结果只能作提示或预取，不能直接作为执行授权。

例如用户当前可承受新增负债 100，向 A/B 各请求借款 80：先取得账户锁的一笔可以通过；另一笔等待后必须看到前一笔新增负债，再检查是否还能借 80，不能沿用原来 100 的额度。能否通过取决于第一笔后的真实组合风险，不硬编码“第二笔必失败”。检查顺序以取得账户锁为准，不承诺跨市场请求的全局 FIFO。

推荐先用已有数据库 User 行锁落实这一要求，不新增账户队列或请求恢复机制；独立借款/还款、现金转出、强平等账户写也必须遵守同一锁纪律。账户锁保护执行检查到资金提交；提交后市场快照短暂未发布的窗口仍按已批准的独立行情语义处理，不能用旧行情中的账户持仓或负债替代刚重读的本人账本。若要求“同一账户连入队都不能并发”，那是额外的请求准入/忙碌拒绝策略，须另行确认，不能与防重复授信混为一项。

市场级短债等风险辅助量仍按现有资金模型读取，实施时逐项核对它是否属于本人的负债或市场投影，不能把报价快照误作完整信用账本。外市场价格变动不再触发“冻结依赖后重试”；本人账户版本、实际持仓变化仍必须重验。

### 2.4 账户、历史与 SSE 组合读取

| 读取类型 | 承诺 |
|---|---|
| context / versions / prices、私人 SSE 首帧 | 账户内容沿用 before/basis/after 的 economic_version 稳定检查；附带价格是独立已提交快照，不等待市场队列 |
| minimum_group_stamps | 仅是本响应附带价格的版本下限，不再表示该账户变更已被行情因果覆盖；前端继续分别合并账户版本和价格 stamp |
| 普通行情/估值 | 最近完整发布快照，不等待正在执行的成交 |
| 历史区间、带成交游标/历史前缀的公共 SSE 组合包 | 保留既有历史覆盖、去重、封存、高水位约束；不拿一份旧报价配一段新历史冒充同一截面 |

**历史的具体取舍：本轮保留精确历史读取协议。** FX 相关路径目前用数据库 `FOR SHARE` 及同 session 数据核对历史与池版本；它可能等待进行中的数据库写，但不再经过通用 GATE。纯报价、账户估值不调用该历史组合路径。LMSR ring/tail 与订阅 anchor 约束同样保留。

因此本轮不能声称“所有 SSE 包绝不等数据库”：公共包若同时要求精确历史，仍可能等数据库。让这类包也完全免等待，需要另行把历史 cursor/前缀一起纳入 writer 发布，或拆开行情与历史协议；本轮推荐先保留当前协议，避免把交易调度迁移扩大成历史协议重做。

账户通知统一放在目标快照安装之后，减少客户端无谓刷新；它不是跨账户、跨市场的原子一致性保证。现有 JSON 结构优先不变，更新 schema 注释、协议说明及前端 store 注释；若核查发现其他消费者依赖旧覆盖承诺，先报告，不静默变更接口。

## 3. FX 全入口收敛表

| 入口 | 推荐落点与约束 |
|---|---|
| 现货、融资买入、净指令、开空/平空 | FxWriter 命令 → 现有 `_in_session` 内核；writer 统一提交和安装 |
| 条件单触发成交 | 同 pair 命令；出队后锁订单重验状态，成交和订单终态同事务；不能持订单/账户锁等待 submit |
| 条件单创建/取消/失败终态 | 同 pair 命令；与触发成交按同队列排序，仍保留订单锁和终态重验。scheduler 只发现候选并提交命令，不自行提交失败状态 |
| FX 强平 | 锁外发现目标、结束发现事务后 submit；handler 锁内重新 prepare 和校验，资金/幂等记录同事务 |
| 注资/撤资 | 入同 pair 队列；拆出现有自提交边界，保留资金、审计及历史事件同事务 |
| 管理改费率/储备/状态、归档、删除 | 入同 pair 队列；删除发布失效状态，后续旧命令按不存在/不可用返回，不创建新 actor |
| 新建市场 | 新 ID 无已有交易队列；保留容量管理锁与创建事务，commit/关闭 session 后 bootstrap，成功后才接经济命令 |
| 重载/已有市场重新注册 | 排入已有 writer；consumer 内异常恢复直接调用私有 reload，不 submit 给自己 |
| PvE 实盘交易与强制平仓 | 复用同一交易命令；当前 before_commit 的 profile 更新继续留在成交事务内 |
| PvE observation-only | 现有路径已锁 pair 并可能影响随后实盘判断，先纳入同 pair 命令；handler 使用无提交内核，不能回调会 submit 的外层 wrapper |
| PvE 账户生成、充值、策略启停/退休 | 不改 AMM 的账户与策略管理继续独立事务，保留 Pair/User/Profile 锁和执行时状态重验，不承诺它们与市场命令排队顺序一致。初始钱包分配继续按现有规则处理，不新增资金来源；实际成交与 observation 仍按上两行入队 |
| 管理员核销 FX 空仓 | 虽不一定改池价，仍涉及 pair 经济状态与用户/金库，入同 pair 命令 |
| 全局配置、公告、纯资料编辑 | 保留独立管理路径；若实际改变已发布经济输入，需列入相应市场命令，不能作为普通资料直接写入 |
| 离线 reset / 修复脚本 | 继续要求 backend 停机后运行，不提供在线绕过 writer 的维护入口 |

跨市场批处理逐市场提交，不由一个 actor 持锁等待另一个 actor。保留已有批处理的部分成功/返回语义；若原需求要求跨市场全有或全无，属于新的决策点，不能靠嵌套队列模拟。

## 4. 文件职责与接口边界

- 新增 `backend/app/services/fx/writer.py`：FxWriter 的每 pair queue、consumer、生命周期和 submit；只管顺序，不复制 AMM/借款算法。首轮沿用 LMSR 的队列容量 256、等待 10 秒及排队拒绝/结果未知语义；未开始且等待方已取消的命令可跳过，已开始的命令可能提交，不声称超时意味着回滚。
- 新增 `backend/app/services/fx/writer_ops.py`：类型化命令处理和事务边界，复用交易内核、构造最终投影；命令仅携带 ID、金额、请求参数，不能携带外部 AsyncSession/ORM 对象。
- 修改 `fx/{trading,borrow_buy,net_trading,shorts,orders,liquidity}.py`、`credit/execution.py`、`pve_fx/executor.py`、`admin_user_service.py`：外层收敛 submit，内层仅在 writer 所有的 session 中执行，禁止嵌套提交。
- 修改 `api/v1/{fx,admin_fx}.py`、`main.py`：请求连接释放、启动 bootstrap/关闭生产者与 writer；管理入口转发命令。
- 修改 `market_writer.py`、`writer_ops.py`、`account_events.py`：LMSR 生命周期入队、提交后 apply/install、随后通知；不另建通用 actor 框架。
- 修改 `trading_prices.py`、`market_read_state.py`、`credit/{risk,trading_context}.py`：普通读免排队、冷读不安装、风险复制已发布输入；复用现有 frozen dataclass/tuple。
- 修改 `schemas/trading_context.py`、前端 `tradingSession.ts` 的相关协议说明；不预设需要重写 UI。
- 最后清理 `credit/gates.py` 及剩余调用；保留 OWNERSHIP、资金行锁、经济版本、历史协议。历史文档中的旧设计注明已迁移。

锁纪律：FX 继承实际生产的 Pair → User → 钱包/空仓/金库顺序；管理容量操作在 handler 内先取得现有 capacity 保护，再锁 Pair，调用者不能持 capacity 等待 submit。多用户按稳定 ID 顺序。LMSR 保留已有真实事务锁顺序，并核对结算路径。所有 producer 必须在 submit 前结束事务、释放锁与连接；所有 consumer 禁止 await 其他市场 writer。不以一条过时的锁顺序注释替代代码审计。

PvE observation 和成交保留当前两次事务的业务时序，提交两条命令之间不持锁；成交命令重新验证 profile/episode 状态，on_fill 仍在成交事务内。删除成功后停止接收该 ID 的新经济命令，已排队命令明确失败并完成其 future，不能留等待者悬挂；不能用 register 自动复活旧 ID。

## 5. 分阶段实施与验收

### 阶段一：FX 唯一写入归属

- [ ] 建立 FxWriter 及生命周期，先迁移普通交易、融资净指令与开平空，复用现有内核；迁移期间保留原 GATE 作为过渡保护。
- [ ] 收拢条件单、强平、注撤资、PvE 和管理写入；拆开自提交 helper，记录每个 commit 的实际归属。
- [ ] 统一 writer 的 commit → 同步 install → 通知顺序；投影失败沿用 unavailable/从 DB 重载方式，禁止提交成功后继续用未更新状态处理下一笔。
- [ ] 核对生产调用图，确认活跃 pair 经济写入没有绕过 writer，再做阶段性提交。

验收重点：同市场命令实际串行；借款和成交仍同事务；两市场同用户现金/债务不丢失；异常不发布未提交快照；删除/重载不能与成交交错；没有持锁等待队列和 consumer 自 submit。

### 阶段二：LMSR 补齐生命周期与通知顺序

- [ ] 新 ID bootstrap 后公开 actor；已有 ID 的 register/reload 入原队列，恢复在 consumer 内直接执行。
- [ ] 将 op 内 commit 后的账户通知材料交回 consumer，在 apply/install 后发出；tick 与 future 完成顺序一并核查。
- [ ] 保留 OWNERSHIP、unavailable、幂等强平、User/Position 事务保护，阶段性提交。

验收重点：已有市场重载不覆盖较新 q；恢复不等待自己；买卖、结算、强平原有资金与历史断言保持。

### 阶段三：切换快照读取与风险口径，移除 GATE

- [ ] 普通 context/versions/prices/私人 SSE 与 quote 使用不可变已发布快照；移除读 GATE 和冷读全局安装。
- [ ] 风险在锁定账户后取得本次已发布跨市场输入；本账户与实际执行目标仍重验，移除外市场冻结与相应 held_keys 前提。
- [ ] 保留公共历史组合路径的真实前缀保护，更新账户附带价格的协议说明与旧等待语义测试。
- [ ] 审计剩余 GATE 调用归属，确认各项替代已落地再删除通用 GATE；不能只靠搜索无引用判定资金正确。
- [ ] 更新交易指南与迁移记录，说明旧图失效位置；阶段性提交。

验收重点：市场写执行中普通读可完成并返回上一份完整投影；抵押市场写执行中借款不等其队列；本人并发借贷仍串行；冷 SQL 迟到不覆盖/复活；历史完整性不放宽。

账户并发验收复用 PostgreSQL admission contention 用例，明确覆盖两个不同市场 writer 同一用户同时融资：只有持锁的一笔能检查/变更本人资金，后取得锁的一笔使用已提交的新债务/持仓重算；另检查跨市场持仓新增/清空后旧依赖预取不能漏算。保留账户版本、总债务与现金实际断言，不只断言某个锁函数被调用。

### 阶段四：集中审阅与同口径性能对照

- [ ] sol 独立审阅实际产物与入口覆盖，修复具体资金/顺序错误，不扩展新机制。
- [ ] 集中运行下面相关检查；局部修复只补跑受影响检查。
- [ ] 使用同机器、相同初始化数据、100 用户/200 SSE 与原请求密度，依次比较固定 `330f4e6` 和最终提交；复用原读取型、活跃交易及压力阶梯与保护停止条件。
- [ ] 分别报告成功吞吐、错误率、行情/context P95/P99、市场队列等待/执行时间、User/DB 锁等待、SQL/CPU；不把取消/失败请求算成功吞吐。

单市场实验主要验证普通读解耦；另用小规模真实双市场并发用例验证借款不冻结抵押市场，不为了这点搭建新压测平台。A/B 数据目录与之前结果分开，不把中间草稿测试当作最终验证。

## 6. 验证范围与可修改的旧断言

已有测试优先复用：

```bash
# cwd: backend；使用项目 venv。仅在实施完成后执行，本次方案没有运行这些检查。
venv/bin/python -m pytest -q tests/test_market_read_state.py tests/test_trading_context.py tests/test_trading_stream.py tests/test_market_writer_loop.py tests/test_market_writer_state.py tests/test_writer_buy.py tests/test_writer_sell.py tests/test_writer_admin_ops.py tests/test_writer_e2e.py tests/test_credit_risk.py tests/test_credit_short_risk.py tests/test_credit_execution.py tests/test_credit_liquidate_group.py tests/test_fx_trading_service.py tests/test_fx_trading_in_session.py tests/test_fx_borrow_buy.py tests/test_fx_net_trading.py tests/test_fx_short_trading.py tests/test_fx_short_liquidation.py tests/test_fx_trigger_orders.py tests/test_fx_pve_execution.py tests/test_admin_fx.py tests/test_admin_fx_multi_pair.py

# TEST_PG_DATABASE_URL 必须明确指向一次性本机测试库，缺配置/skip 不算通过。
venv/bin/python -m pytest -q -m pg tests/pg/test_pg_trading_context.py tests/pg/test_pg_credit_admission_contention.py tests/pg/test_pg_fx_short_concurrency.py tests/pg/test_pg_credit_liquidation.py tests/pg/test_pg_fx_trigger_orders.py tests/pg/test_pg_fx_history_locking.py tests/pg/test_pg_writer_ownership.py
```

实施时受影响的流动性、账户通知、writer tick/history/ring 检查按实际改动补入，不无理由重跑全仓。前端协议涉及 store 时在 `thccb-frontend` 运行 `npm run test:unit -- src/stores/__tests__/tradingSession.spec.ts`。最后运行 `git diff --check`；无 UI 行为变更则不做浏览器交互测试。

允许替换的断言：普通读必须等市场写、借款必须冻结其他市场、账户与价格必须来自同一提交截面、普通冷读必须安装全局缓存。替换后要验证新可观察行为：及时返回完整旧快照、未来发布可更新、本人钱债不重复、未提交数据不外泄。

不允许放宽：资金守恒、债务/持仓准确、滑点限制、账户版本、同事务账本/订单状态、幂等、历史无缺口/重复、所有权拒写。新增永久测试只补同市场排序、跨市场放行、重载/冷读覆盖这类真实缺口，优先放入已有测试文件；不用源码字符串断言代替行为。

## 7. 取舍与批准边界

推荐批准的是：**FX 局部 writer + LMSR 生命周期补齐 + 普通不可变快照读取 + 执行时跨市场估值**。保留数据库成交权威、账户锁/版本、现有写所有权和精确历史协议。

收益是取消读者与外市场交易的耦合，收敛发布时序；代价是承认账户/行情并非原子截面，并增加 FX 调度代码。单市场写仍串行，吞吐改善程度必须由实验决定，不能预先保证。

不在本轮实施：全内存撮合/账本、入队即保证授信、跨市场原子事务、新风控预留系统、历史协议重做、跨进程报价广播或新恢复框架。若业务实际需要这些能力，再单独提交方案。

用户已确认执行检查时间点、历史取舍与现有 User 行锁方案；实施进展及实际验证记入本计划的 SDD ledger。草稿 6526700 仍仅作参考，不直接整批合并。
