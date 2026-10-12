# LMSR 内存行情与恢复 Implementation Plan

> **For agentic workers:** 本计划当前延期待办，用户明确恢复后才进入执行流程。恢复时使用 superpowers:executing-plans 按阶段执行；允许 Sol medium 阶段审阅，遵循架构确认与测试范围约定。

**Goal:** 统一 LMSR 当前行情快照，分离行情整理与经济 writer，并建立可恢复的行情持久进度。

**Architecture:** 阶段 1 在现有 writer 上提供不可变读取快照。阶段 2 推荐独立 LMSR 行情服务与持久游标，SQL 中已提交记录驱动增量图表；经济 q 和每笔事务边界不变。FX 内存 writer 仅待办。

**Tech Stack:** 现有 FastAPI / SQLAlchemy / SQLModel / Alembic / PostgreSQL / SQLite；不新增依赖。

**Spec:** [2026-10-12-lmsr-market-data-design.md](../specs/2026-10-12-lmsr-market-data-design.md)。基线 `4893853`，分支 `feat/fx-net-trading`。

状态：**延期待办，三个阶段均未实施。** 用户于 2026-10-12 明确表示短期不实施，要求保存任务以后继续；当前只保存方案与计划，不启动实现。新只读模块边界、阶段 2 的游标表、独立行情模块及初始化方式见设计“实施前需确认的架构决策”。恢复后确认这些具体方案；已确认内容不重复询问。

## 以后恢复时从这里开始

1. 阅读本计划和配套设计，核对最新代码与基线 `4893853` 的差异；本轮只准备了文档。
2. 确认阶段 1/2 的模块、游标表和首次初始化方案，再从 Task 1 执行；保留既有资金/风控和每笔 SQL 提交。
3. FX 内存 writer 继续保持阶段 3 待办，需独立确认，不能随阶段 1/2 自动实施。

## Global Constraints

- 保留现有 LMSR 定价、6 位资金/份额、8 位价格及历史列式编码，四档仍为 10s/1m/15m/1h。
- 保留经济写所有权、品种门闩、用户锁、风控、审计、每笔 SQL 提交、提交成功后更新经济内存。
- 不恢复非 writer 经济写，不添加 legacy/new 运行开关，不将资金、持仓异步落库。
- 保留列表/详情筛选、权限与 HTTP 字段，SSE anchor、gap、心跳、重连和成交 ID 去重。
- 只读实例、已结算市场和超出 ring 窗口的读取保留 SQL；不依赖只读实例拥有 writer。
- 尊重既有 `docs/architecture.md`、`docs/fx.md` 和 FX read-cost 计划的未提交内容；不覆盖或混入无关提交。
- 优先已有测试；不为拆文件、写文档或计划新增测试。具体关键行为有缺口时先注明真实故障和最合适覆盖层，再决定是否补，不建独立验证工程。
- 使用独立临时数据库，不连接生产库、不启动浏览器交互。提交、推送和部署按用户另行指令执行。
- 一个阶段完成并通过相关检查后再进下一阶段，不让阶段 1 依赖阶段 2 的 schema。

## Review Focus

1. 内存份额、价格、status、b、截止时间同一时刻复制，后续 await 不观察可变引用。
2. 只读/已结算读取仍可用；活跃 state 不可用时不发布看似正常的价格。
3. 非均匀种子份额、跨桶、强平夹在普通买卖之间，重放保持正确价格和成交量。
4. 同批重放、提交结果未知、游标缺口、首次初始化失败不重复累计或毁掉旧历史。
5. ring 落后 writer 时首包 tail 和覆盖 ID 一致；未完整段不返回 immutable 200。

## 文件职责

| 文件 | 计划职责 |
|---|---|
| `services/lmsr_market_reads.py`（新增） | 不可变当前市场快照和 API 只读投影；避免与现有 `services/lmsr.py` 同名包冲突 |
| `api/v1/market.py`、`api/v1/stream.py` | 使用统一快照，保持元数据/权限 SQL 读取和现有协议 |
| `services/market_writer.py`、`writer_ops.py` | 经济 state、事务和队列；阶段 2 移出 candle 计算/落库 |
| `services/lmsr_market_data.py`（阶段 2 新增） | ring、成交增量消费、只读 tail、处理/保存水位 |
| `models/lmsr_market_data.py`、Alembic 新迁移（阶段 2） | 经确认的持久进度表 |
| `services/candle_flusher.py`、`candle_writer.py`、`history_ring.py` | 可靠批次、复用纯聚合和编码；避免与 FX 同时做通用框架重构 |
| `api/v1/history.py`、`main.py` | 读取完整性、启动补齐、自愈与停机生命周期 |
| 现有 writer/stream/history/candle/market 测试 | 调整真实生命周期与断言，保持单层覆盖 |

## 阶段 1：统一当前行情读取

### Task 1：冻结快照边界与已有行为

- [ ] 确认阶段 1 模块边界；记录目标工作树、已有改动和基线。
- [ ] 盘点列表、详情、quote、SSE 首包每个字段的来源；区分实时字段与目录/用户权限/历史统计。
- [ ] 复用已有 fixture，对真实请求保存响应与 SQL 计数；临时脚本/JSON 放 `/tmp`。覆盖活跃、HALT、已结算、只读和 unavailable。

**验收：** 字段来源清楚，基线可复现；不创建永久性能阈值。

### Task 2：实现不可变快照并接入读取入口

**Interfaces:** `MarketReadSnapshot` 与 `read_live_snapshot(market_id: int) -> MarketReadSnapshot | None`，字段见设计。阶段 1 可提供同次捕获的 tail/覆盖 ID；阶段 2 由协调读取接口完成 tail 补齐，API 投影规则不复制。

- [ ] 新建 `lmsr_market_reads.py`，一次无 await 地复制份额、量化价格、状态、b、截止时间和当前行情边界。
- [ ] 市场列表、详情、SSE 首包使用该快照；SQL 继续取必要元数据、权限和统计，不重复从 SQL q 算当前价。
- [ ] 保留只读/已结算 SQL 读取；活跃 state 异常明确拒绝。SSE 在 SQL 查询后获取快照，保持订阅 anchor 顺序和首包去重。
- [ ] 校验 SQL 元数据和快照的生命周期字段；不一致时结束旧读事务并刷新实体后有界重读，避免旧胜出/结算信息配新 status。阶段 1 不新增 schema，重试耗尽明确拒绝。
- [ ] `quote` 已读 writer 的路径仅在有必要时复用快照，不顺手修改报价缓存时效或滑点规则。
- [ ] 运行已有 `test_market_priors.py`、`test_core_audit_3.py`、`test_stream_history_tail.py`、`test_stream_build_hash.py`、`test_api_stream_namespace.py`、`test_history_endpoint.py` 及实际受影响列表/准入用例。
- [ ] 对同一临时基线核对响应及 SQL/定价调用变化，分别报告实时读取收益与仍保留的 SQL 成本；Sol medium 只审阅该阶段 diff。

**验收：** 实时字段同源，已有边界行为正确；不把阶段 1 宣称为全端点零 SQL。

## 阶段 2：行情独立处理与持久恢复

### Task 3：确认存储/模块方案并预检历史来源

- [ ] 用户确认设计推荐的游标表、行情边界与首次离线基线方案。
- [ ] 核对 `Transaction.id` 的同市场执行顺序、完整价格快照、初始份额、强平及结算记录；梳理 SQLAdmin/维护工具等特殊来源，输出可重建与需修复清单。
- [ ] 确定每市场页大小、批次上限、启动追赶预算及首次基线预计耗时，避免在计划中猜生产规模。
- [ ] 明确旧 immutable 缓存的失效办法；如需要变更公开历史 URL 或响应字段，先提交决策点，不在实现中自行扩范围。

**验收：** 历史来源可确定重建，架构与初始化方案已确认；未满足时只继续独立调查，不修改 schema。

### Task 4：持久游标与可靠 candle 批次

**Interfaces:** `LmsrMarketDataState` 保存设计字段；内部批次携带 market_id、history_version、expected_cursor、through_cursor 和 candle 行。`apply_lmsr_candle_batch(session, batch) -> bool` 只在调用方事务内工作，bool 表示是否新应用；重复返回 False，断档/代际冲突拒绝。

- [ ] 新增模型和 Alembic 迁移；迁移只建进度表，不删已有 candle，不把当前 migration 位置固化为测试断言。
- [ ] 在同一事务内保存 candle 增量与 cursor；基于检查点处理重复、重试及未知提交结果，不复用旧“失败就把累加批次再刷一遍”的做法。
- [ ] 实现离线每市场建基线，失败事务回滚旧数据；空市场初始化 ready，缺来源停止并报诊断。
- [ ] 复用已有 candle 聚合/落库/确定性用例和迁移辅助工具验证真实 schema、数据保留、重放与失败恢复。新不变量若无覆盖，登记缺口，按用户测试约定选择最小覆盖，不机械新增每层用例。

**验收：** 持久 candle 和进度一致，同批不会重复累计；离线初始化可恢复且不掩盖缺数据。

### Task 5：增量行情服务和交易队列分工

**Interfaces:** `LMSR_MARKET_DATA.start(write_owner: bool)`、`stop()`、`notify_committed(market_id: int)`（同步非阻塞提示）、`catch_up(market_id: int, through_transaction_id: int | None = None)`、只读 tail/segment 读取。具体返回对象复用已有编码；服务不调用交易 writer。

- [ ] 新增 per-market consumer/ring 与 applied/queued/durable 水位，通知合并、分页和周期 SQL 补偿。
- [ ] 按真实已提交记录生成普通买卖 candle，强平只推进价格上下文；保留原成交量和结算语义，来源异常拒绝进入 ready。
- [ ] 移出 `writer_ops.compute_candle_rows` 和经济 `_apply_outcome` 中 ring/flusher 工作；writer 提交成功后只更新经济 state、给出稳定边界、投喂 tick 并发出非阻塞提示。
- [ ] 保留行情任务的有界处理，交易不等待图表追赶；SQL 消费成本计入整体性能评估，不把任务移出队列等同于消失。
- [ ] 调整已有 writer ring/E2E 和 candle 用例，让它们等待真实行情 consumer/flusher，保留资金/份额/审计断言；Sol medium 核对提交边界和完整写路径。

**验收：** 交易不承担 candle 聚合和持久化等待，真实成交仍每笔 SQL 提交；ring 最终与持久结果一致。

### Task 6：首包、不可变历史与生命周期

- [ ] 接入 `stream.py` 和 `history.py`：捕获 writer 边界 N，tail 补到 N 后一并输出覆盖 ID；补齐不持经济锁，生成器失败关流重连。
- [ ] tail 严格截至 N；consumer 已超过 N 且聚合不能截断时，重新捕获经济快照和历史覆盖边界，不只改 ID。尾段/水位用不可变结果返回，超过等待预算拒绝或重连，不允许 N 之后的 candle 混入旧首包。
- [ ] 只有封存且处理/持久进度覆盖的段可以供给 immutable 200；SQL/内存落后不能发布缺桶缓存。
- [ ] 接入新市场注册、已结算历史、只读实例、经济 state 自愈和优雅停机。写 owner 启动时完成持久检查点缺口补齐；只读实例只读取，不推进持久数据。
- [ ] 确认新管道完整后删除旧启动窗口重建和重复落库路径，不保留切换开关。
- [ ] 复用现有 stream/history/candle/resync/writer 和相关 PG 并发检查。最后一次相关改动后运行项目要求的后端 compile/import、完整非 PG 套件；PG 使用独立临时库，不把跳过算通过。
- [ ] 对同池、同账户/选项数量比较交易队列等待、响应、SQL、行情延迟与数据结果；只做一次性采样，不加入永久压测框架。若成本反而显著上升，收缩方案并记录原因。
- [ ] Sol medium 最终审阅，更新架构解释与验证记录；相关检查通过后结束，不机械追加浏览器流程或重复全套。

**验收：** 首包不丢成交、不重复成交量，历史缓存完整；重启靠检查点恢复；性能收益和代价有实际证据。

## 阶段 3 待办：FX 内存 writer（本轮不实施）

- [ ] 先测 FX 成交中储备读取、池锁等待、账户/全仓风控、账务与提交的成本占比。
- [ ] 单独提出 `FxPoolState` 与 per-pair 经济 writer 的方案；候选字段为 gold_reserve、foreign_reserve、费率、status/reduce_only/archived、pool_version。
- [ ] 列出并统一所有池写入口：普通交易、跨零、开空/回补、强平、流动性、管理员状态/费率；明确队列与现有跨品种门闩的关系。
- [ ] 明确组合风控快照版本、提交成功后内存 apply 失败的隔离恢复、重启和单写所有权；保留用户/钱包/欠币/回执/审计的每笔 SQL 事务。
- [ ] 展示方案取舍和证据，用户确认后再写独立实施计划；不因本轮阶段 1/2 计划获准而直接改 FX 经济写边界。

## 计划自检

已覆盖当前内存字段与 SQL 元数据分工、种子/强平价格上下文、首次基线防重复累加、未知提交结果、首包落后补齐与 immutable 完整性。阶段 1 独立于新 schema；阶段 2 的新表、模块和初始化明确待确认；阶段 3 只有待办。Sol medium 只读审阅认为整体可交付；两项关键歧义已按审阅结果补充到 Task 2/6。本文尚无测试通过或性能提升声明。
