# FX 行情性能优化 Implementation Plan

> **For agentic workers:** 按任务边界使用 `superpowers:executing-plans` 或用户选择的 `superpowers:subagent-driven-development`。下列文件职责是建议的所有权边界，选择委派时必须将全局约束传入每个 prompt；不能因本文出现委派方式而自动启动子 agent。

**Goal:** 将 FX 行情读写迁移到 LMSR 已验证的增量聚合、物化 K 线、历史缓存和 SSE 路线，消除主页与后台重复加载全日成交对象的 CPU 成本。

**Architecture:** 原始 `FxTrade` 和既有经济事务仍是真实来源。提交后轻量通知独立 FX 行情服务，按 pair 游标维护 ring/K 线，批量落库与游标一起提交；HTTP/SSE 从派生结果读取，网络推送失败不影响聚合完整性。复用 LMSR 的模块模式及纯工具，使用 FX 的金额、价格编码和命名空间适配器。

**Tech Stack:** 现有 Python/FastAPI、SQLModel/SQLAlchemy async、Alembic、PostgreSQL/SQLite、Decimal、pytest；Vue 3/TypeScript、Vitest、lightweight-charts；现有 nginx。

**Spec:** [2026-10-01-fx-market-data-performance-design.md](../specs/2026-10-01-fx-market-data-performance-design.md)。本文是已确认技术路线的执行草案；本轮仅编写文档，未开始实施或授权部署。

## Global Constraints

- 保留 AMM、钱币债务、审计、OWNERSHIP、GATES 与 pair→User→钱包/空头→treasury 行锁序；行情缓存不能参与风控和下单准入。
- 所有真实 FX 成交均入聚合；回滚、拒单、幂等 replay 不重复入桶。提交后才更新行情内存或发通知；可靠聚合与可丢弃推送分开。
- OHLC 按 `(created_at,id)` 的 `post_price`；成交量为 buy 金入/sell 金出。FX 历史 OHLC 字符串编码，保留 Decimal，图表边界才转 number。
- 周期 `10s/1m/15m/1h`，flush 与遗漏通知核对每 5 秒；持久游标和桶同事务落库，重建变更历史版本。精确滚动 24h 成交量不能忽略窗口边界或时间过期。
- 保留 `/snapshot`、`/chart` 与旧 `fx` SSE 字段；新历史命名空间为 `/history/fx/`，响应最多 20,000 个桶。LMSR 的既有路径与缓存内容保持兼容。
- 不增加框架、依赖、进程或全站经济锁。新发现的接口/存储/部署路线决策先提交用户选择；工作区多人协作时不撤销他人修改。
- 相关已有检查默认运行；新增永久测试只补具体故障缺口，不为文案/布局/删除测试机械加测。页面验证最多截图，不自动点击/填表。推送和部署须有该阶段用户授权。

## Review Focus

| 易漏条件 | 必须保证 | 任务 |
| --- | --- | --- |
| 系统 tick、新闻首轮冲击、强平卖出/回补走不同提交入口 | 各类真实成交均可从游标恢复，通知失败不漏历史 | 2–3 |
| flush 提交结果不明、重启超过一小时、重复回填 | 数据库游标与桶一致，成交量不重复、不漏记 | 2–3 |
| UTC 桶边界、同时间多笔、跨批次先后、24h 窗口移动但无新成交 | O/C 与真实排序一致，滚动成交量自然过期 | 2、4 |
| snapshot 读到已推送成交、断线 gap、publisher 合并/溢出 | 按成交 ID 去重或明确补尾部，不重复计算成交量 | 4–5 |
| 未 flush 的封存段、全量重建、赛季后 pair ID 重用 | 不完整段不返回可缓存 200，旧版本缓存不污染新历史 | 3–4 |

## 文件与职责

| 职责 | 主要文件 |
| --- | --- |
| 快照与实时发布 | 修改 `backend/app/services/fx/{trading,publisher,market_data,engine,scheduler}.py` |
| 派生表、桶计算与批量持久化 | 修改 `backend/app/models/fx.py`；新增 Alembic revision、`backend/app/services/fx/candles.py`、`backend/app/services/fx/candle_flusher.py` |
| ring、应用/持久游标、补读与快照尾部 | 新增 `backend/app/services/fx/market_state.py`，复用 `backend/app/services/history_ring.py` 的工具/模式 |
| 成交提交接入 | 修改 `backend/app/services/fx/{trading,shorts,engine,scheduler}.py`、`backend/app/services/credit/execution.py` |
| 历史、图表与 SSE | 修改 `backend/app/api/v1/{fx_stream,history}.py`、`backend/app/schemas/fx.py` |
| 启停、回填、重置、nginx | 修改 `backend/app/main.py`、`backend/scripts/season_reset.py`、`deploy/nginx.conf`；新增 `backend/scripts/backfill_fx_candles.py` |
| 前端 | 修改 `thccb-frontend/src/{api/fx.ts,types/fx.ts,pages/Fx.vue,components/home/FxOverview.vue,components/chart/FxCandleChart.vue}`；新增 `composables/useFxCandleHistory.ts`，按需复用/提取 `useCandleHistory.ts` 的纯工具 |

## Task 1：先降低快照与系统发布 CPU

**接口：** 保留 `get_public_snapshot(db,pair_id)` 与 `enqueue_publication(pair_id,post_price,trade_id)`；`publish_trade(trade,broker=None)` 保留显式测试 broker 的直接发布适配，生产调用归入 bounded publisher。

- [ ] 复用快照成交量测试，覆盖窗口内外的 buy/sell 与空结果；SQL `SUM(CASE ...)` 返回与原口径相同的 Decimal，不读取 `FxTrade` ORM 集合。
- [ ] 系统 tick 和新闻冲击统一提交后排队；无人订阅跳过快照，发布失败不影响成交结果，不把网络发布留在 pair GATE 中。保留现有新闻推送行为。
- [ ] 运行：`cd backend && venv/bin/python -m pytest -q tests/test_fx_trading_service.py tests/test_fx_publisher.py tests/test_fx_engine.py tests/test_fx_events.py tests/test_fx_stream.py`。新增断言应针对真实成交/成交量或发布行为，不固定内部调用次数。
- [ ] 在固定规模隔离 PG 上记录单快照和无人订阅 tick CPU，确认不再构建全日成交对象。提交本包文件；不推送部署。

## Task 2：派生表与可靠增量聚合

**依赖：** Task 1。**接口（已按实际实现更正）：** `compute_fx_candle_rows(trades) -> list[dict]`；`FX_MARKET_DATA.notify_committed(pair_id: int) -> None`（同步、只标记 dirty）；`await FX_MARKET_DATA.catch_up(pair_id: int) -> int`（返回内存应用游标）；`await FX_MARKET_DATA.flush_once() -> int`（**runtime 持有的** `FxCandleFlusher` 刷盘入口；不存在全局 `FX_CANDLE_FLUSHER`，`FX_MARKET_DATA.flusher` 只是 sealing 检查用的只读句柄）。`market_state.py` 拥有 per-pair 串行消费、ring、应用游标和待落库批次；flusher 拥有桶/持久游标同事务写入。

- [ ] 建 `FxCandle`、`FxMarketDataState` 和 `(FxTrade.pair_id,FxTrade.id)` 索引；新迁移只增加派生结构。迁移测试放在现有 FX migration 用例，验证原始成交/账户数据保留、约束及派生表回滚。
- [ ] 四周期桶遵循 FX 口径与首末排序键；复用 ring 的窗口/封存常量，FX codec 保留价格字符串。消费增量只投影必要列，按 pair 高水位取已提交成交，不加载全日 ORM 对象。
- [ ] 定时核对高水位补偿丢通知；5 秒 flusher 原子更新 K 线与持久游标，重复/不明提交先确认游标。内存与持久状态分开，推送合并不能丢聚合数据。
- [ ] 在已有 `test_fx_market_data.py` 扩展真实桶行为；必要的新覆盖集中 `test_fx_candle_pipeline.py`：跨批次、重复 flush、失败恢复与遗漏通知。运行：`cd backend && venv/bin/python -m pytest -q tests/test_fx_market_data.py tests/test_fx_migration.py tests/test_fx_candle_pipeline.py`。
- [ ] 通过后提交模型、迁移、聚合模块与其测试。本包不用改变现有公开读取路径。

## Task 3：所有成交入口与恢复生命周期

**依赖：** Task 2。**接口（已按实际实现更正）：** `await FX_MARKET_DATA.start(write_owner: bool)`、`await FX_MARKET_DATA.stop()`、`FX_MARKET_DATA.reset()`；`await rebuild_pair(db,pair_id,through_trade_id) -> int` 用于派生数据重建（实际位于离线脚本 `scripts/backfill_fx_candles.py`）。原始成交截止点、桶替换、持久游标和新历史版本必须在受保护的 pair 维护边界内一致切换。

- [ ] 现货/空头/系统/新闻/强平的事务拥有者提交后调用 `notify_committed(pair_id)`；事务内执行器保持不 commit、不更新行情内存。依靠定时补读覆盖 commit 后通知前的崩溃，不把正确性绑定到 request callback。
- [ ] startup 先确认 ownership，再补齐全部游标差额并预热 ring，然后启经济生产者；读取实例不 flush。shutdown 按 spec 排空和落库；赛季重置先清派生子表，再删 pair，并清内存/缓存。
- [ ] 回填脚本支持指定 pair、分批读取、截止 ID 和明确重建；首次回填在上线读取切换前完成，不能把生产全量回填塞进 Alembic。正常恢复按持久游标补读，不限定一小时。
- [ ] 复用 `test_fx_short_trading.py`、`test_fx_liquidation_service.py`、`test_fx_events.py`、`test_fx_season_reset.py` 的真实成交夹具；聚合缺口在 pipeline 用例集中覆盖。PG 在 `tests/pg/test_pg_fx_market_data.py` 仅补独立竞态：同 pair 提交与消费高水位交错、回滚、flush 提交不明；使用真实 ownership/GATES 和一次性测试库。
- [ ] 运行上述相关测试及 `TEST_PG_DATABASE_URL=<一次性测试库> venv/bin/python -m pytest -q -m pg tests/pg/test_pg_fx_market_data.py`。记录 PG 未验证时的限制，提交本包。

## Task 4：物化读取、历史缓存与 SSE

**依赖：** Task 3。**接口：** `await read_fx_chart(db,pair_id,interval,start,end)` 返回旧 `/chart` 字段；`await read_fx_history(db,pair_id,history_version,interval,segment_epoch)` 返回 FX 列式段；`FX_MARKET_DATA.tail(pair_id,now)` 返回版本、尾段及覆盖游标。在 `FxStreamSnapshot` / `FxPublicFrame` 中定义 spec 的新增字段。

- [ ] `/chart` 先校验区间、周期和最多 20,000 桶，再查询聚合表并按桶合并内存尾部；不能直接拼接造成重复成交量。准备未完成时显式重试，不退回原始成交重扫。
- [ ] `/history/fx/{pair_id}/{history_version}/{interval}/{segment_epoch}.json` 采用 ring、flush 高水位、LRU 和 immutable 防线；产品及版本进入所有 cache key，旧 LMSR key/接口保留。
- [ ] 精确 24h 量改为分钟桶＋两端 SQL 聚合补算；无新成交时也淘汰过期量。报价仍读权威 pair，管理员注撤资/费率/状态变化不能被行情内存遮蔽。
- [ ] SSE snapshot 保留 anchor 顺序并携带尾段覆盖游标；`fx` 增量帧按真实时间包含公开成交量和 ID。复用有界 publisher 合并；容量不足显式要求补尾部，新闻和旧价格字段保持兼容。补齐 nginx FX SSE 关闭缓冲和长连接配置。
- [ ] 复用 `test_fx_stream.py`、`test_fx_stream_chart.py`、`test_realtime_namespace.py`；新 FX 分段测试验证封存边界/flush 未完成拒缓存/版本切换，避免重复复制全部 LMSR 场景。运行相关测试，提交本包。

## Task 5：前端采用缓存历史与实时增量

**依赖：** Task 4。**接口：** `loadFxHistoryCandles(pairId,interval,lookbackMinutes,snapshotTail) -> Promise<FxChartPoint[]>`；为 `FxStream` 增加独立 envelope/尾段解析，保留现有 `parseFxSsePayload` 的报价返回契约。所有新增消息须经过严格公开字段校验。

- [ ] `useFxCandleHistory.ts` 复用分段加载、尾段 freshness、版本和缓存逻辑；不可用时回退轻量 `/chart`，LMSR 现有 composable 不改变行为。
- [ ] `FxCandleChart.vue` 从缓存段/尾段初始化，按真实 trade 时间与 ID 增量更新 OHLCV；重复帧不重复累计，gap 补尾段。调整 `Fx.vue` 的转发与 reload 时机，避免每笔成交重新读取整段历史。
- [ ] `FxOverview.vue` 从聚合走势取 24h 基准，报价和走势独立显示；保留已有成功卡片，单项加载失败分别反馈。主页不新增逐卡 SSE 长连接。
- [ ] 复用 `fxCandle.spec.ts`、`fxStream.spec.ts`、`fxOverview.spec.ts` 和已有历史 composable 用例；只补成交量重复/漏记、真实时间分桶及历史版本切换缺口。运行 `npm --prefix thccb-frontend run test:unit`、`npm --prefix thccb-frontend run type-check`、`npm --prefix thccb-frontend run build`；如运行 lint 注意其 `--fix`，只保留本任务相关改动。UI 仅必要截图，提交本包。

## Task 6：性能验收与发布准备

**依赖：** Task 1–5。本包只补实际发现的问题并记录证据，不为验收步骤机械新增测试。

- [ ] 一次性 PostgreSQL 固定三个 pair、每 pair 18,000 条成交，与诊断使用相同时间窗口。分别测单快照、六请求并发、无人订阅/有订阅 tick；报告 wall、应用 CPU、事件循环延迟、SQL/加载行数，profile 运行与正式计时分开。
- [ ] 初始对照目标：快照与无人订阅 tick 应用 CPU 较基线降低至少 90%；FX 25h/15m 重复历史查询应用 CPU 中位数不超过 20 ms、主页六请求事件循环单次延迟不超过 100 ms（同机隔离负载，非生产 SLO）。不达标记录实际瓶颈并修复或提交用户调整目标，不能以 SQLite/跳过 PG 代替验收。
- [ ] 检查全部成交覆盖、SSE 接缝、长时间停机恢复、回填/重建与缓存版本、归档与赛季清理。运行相关保留测试和必要静态检查；遵守 `CLAUDE.md` 的最终检查要求，报告既有警告和未验证项，UI 不自动交互。
- [ ] 发布说明写明“建派生表 → 新版消费启动/回填 → 数据就绪 → 历史/前端读取切换”。回退时回到旧读路径，原始资金账不变；后续再次启用须从可靠游标补齐并刷新版本，不能信任旧写程序期间停滞的派生状态。
- [ ] 记录到 `docs/fx-market-data-performance-validation-<执行日期>.md`，提交对应文件。当前授权是写 spec/plan；实施、推送和部署按后续用户指示执行。

**验证证据（2026-10-01 最终验收，候选 `932dc3a`）:** 已实现契约细化（含审阅者更正）见 spec「实施后契约细化」小节；最终静态/回归/PG/前端命令、结果、限制、未验证项与源码哈希见 `docs/fx-market-data-performance-validation-2026-10-01.md`，公开接口细节同步至 `docs/api.md` 第 12 节与 `docs/fx.md` 行情小节。性能沿用隔离 PostgreSQL harness（三个 pair × 每 pair 18,000 条合成成交）；已在候选 `932dc3a` 源码哈希上以原生 asyncPG 复跑，四项目标全部通过（证据 `task-6-performance-report.md` 与 `performance/results/summary.json`）。whole-branch 审阅 H 已 PASS `932dc3a`；最终 scoped H 审阅 `1a373f7`（**仅** `FxOverview.vue` 的前端 delta）结论为 **SPEC/QUALITY/READY，APPROVED**，所有代码门关闭。注意：44 个 lint error 为既有非 FX 基线，不是 “all green”；G 的 `1a373f7` 上 full front 150 / type-check / build / scoped lint 通过，后端与性能源码哈希重算与 `932dc3a` 逐字节一致（故不触发后端/PG/性能复跑）。**本节的最终验证证据取代上方计划复选框；计划中的命令/路径/接口若与实际实现不同（例如 Task 2 的 flusher 接口、Task 3 的 PG 测试文件名），以实际实现与本节为准。**

## 执行顺序

`Task 1 → Task 2 → Task 3 → Task 4 → Task 5 → Task 6`。前端可在 Task 4 接口确定后准备，但不能抢先猜协议。每包完成其相关检查再衔接；若使用子 agent，独立分配模块所有权并重复携带全局约束。
