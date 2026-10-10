# FX 行情性能：最终验证与证据（2026-10-01）

对应 [设计](superpowers/specs/2026-10-01-fx-market-data-performance-design.md) 与 [计划](superpowers/plans/2026-10-01-fx-market-data-performance.md)。本记录在冻结候选 `932dc3a` 上运行项目现有后端全套、独立 CI FX 端到端、一次性 PostgreSQL 套件、前端单测/类型/构建/只读 lint，并核对 V 的最终性能复跑。所有检查都在本机一次性 SQLite／一次性 PG 库上进行；本记录只描述验证动作，**没有推送、部署、切换线上配置或对生产数据运行维护脚本**（父流程已在本分支完成本地 commit）。

## 候选与源码

- 分支 `ralph/2026-10-01-fx-market-data`。本文所有命令与数字都在**干净工作树 `932dc3a`（`932dc3af4ea4b12aa328e6ed1e2def55cd9778ab`）**上取得。文档更新时 HEAD 已前进到 `1a373f7`（`fix(fx): 报价失败时仍独立刷新走势`，仅改 `thccb-frontend/src/components/home/FxOverview.vue`）。这是 G 的前端-only 修复：**后端/PG/性能被测量源码字节未变**，故本文后端/PG/性能结论对该提交继续成立；本文的前端单测/类型/构建数字只覆盖 `932dc3a`，`1a373f7` 的前端检查由 G 执行并报告通过（见下）。
- 实现基线 `99b9fe2`；性能对照基线为冻结的 `git archive 0a1e12f`（`.../baseline/backend`）。
- 环境：Python 3.13.2（`backend/venv`）、asyncpg 0.30.0、SQLAlchemy 2.0.45、SQLModel 0.0.29、PostgreSQL 14.24（`127.0.0.1:35440`，隔离本地集群）、Node v24.21.0 / npm 11.19.0。
- 一次性库：SQLite `sqlite+aiosqlite:////tmp/fx-md-final-validation.db`；PG `fx_md_validation_test`（本项目验证独占，未打开其他 worker 的库）。
- 关键产品源码 SHA-256（完整清单见 V 的 `performance/results/source-hashes-932dc3a.txt`，验证记录见 `.superpowers/sdd/2026-10-01-fx-market-data-performance/validation/00-recon.txt`）：

| 文件 | SHA-256（前 8 位） |
| --- | --- |
| `backend/app/services/fx/market_state.py` | `7f0cf93f` |
| `backend/app/services/fx/market_reads.py` | `ea2bed88` |
| `backend/app/services/fx/candle_flusher.py` | `9374cf4b` |
| `backend/app/services/fx/candles.py` | `073ec52f` |
| `backend/app/services/fx/market_data.py` | `148243cb` |
| `backend/app/services/fx/publisher.py` | `65ee869c` |
| `backend/app/services/fx/trading.py` | `7b83b759` |
| `backend/app/services/fx/engine.py` | `36229d6e` |
| `backend/app/api/v1/fx_stream.py` | `aad59d12` |
| `backend/app/main.py` | `32cd6f38` |

V 的性能复跑记录的 feature 哈希与上表逐字节一致；相比其上一轮候选 `cbd1206`，只有 `market_reads.py` 与 `fx_stream.py` 变化，因此性能与回归都必须在 `932dc3a` 上重测（V 已重跑，结果见下）。

## 实际命令与结果

| # | 检查 | 命令（`backend/` 或 `thccb-frontend/` 下） | 结果 |
| --- | --- | --- | --- |
| 1a | 后端字节码 | `backend/venv/bin/python -m py_compile $(find backend/app backend/scripts -name '*.py')` | 通过，exit 0 |
| 1b | 导入应用 | `DATABASE_URL=sqlite+aiosqlite:////tmp/fx-md-final-validation.db venv/bin/python -c "import app.main"` | 通过，exit 0 |
| 2 | CI 独立 FX 端到端 | `venv/bin/python -m pytest -q --noconftest tests/test_fx_end_to_end.py` | **18 passed**，3.13s，exit 0 |
| 3a | 现有后端全套（默认非 pg） | `DATABASE_URL=sqlite+aiosqlite:////tmp/fx-md-final-validation.db venv/bin/python -m pytest -x -q` | **1483 passed, 1 skipped, 32 deselected**，246.14s，exit 0 |
| 3b | 现有 PG 套件（独占库） | `TEST_PG_DATABASE_URL=postgresql+asyncpg://sunyunbo@127.0.0.1:35440/fx_md_validation_test venv/bin/python -m pytest -x -q -m pg tests/pg/` | **32 passed**，36.48s，exit 0 |
| 4a | 前端单测 | `npm run test:unit` | **150 passed（14 文件）**，exit 0 |
| 4b | 类型检查 | `npm run type-check` | 通过，exit 0 |
| 4c | 构建 | `npm run build` | 通过，exit 0（仅有既有 >500 kB chunk 提示） |
| 4d | 只读 lint | `npx eslint .`（**未用 `--fix`**，不修改仓库脚本） | **44 errors / 0 warnings**，exit 1；全部在既有非 FX 文件，见下 |
| 4e | 空白检查 | `git diff --check` | 干净，exit 0 |

原始输出保存在 `.superpowers/sdd/2026-10-01-fx-market-data-performance/validation/`：`01-backend-static.txt`、`02-fx-e2e.txt`、`03a-backend-full.txt`、`03b-backend-pg.txt`、`04-frontend.txt`。

### 告警、跳过与 lint（如实记录，未放宽任何期望）

- `pytest.ini` 的 `timeout = 30` / `timeout_method = thread` 产生 2 条 `PytestConfigWarning: Unknown config option`：本 venv **未安装 pytest-timeout**，因此该 30s 硬超时本轮**未生效**（既有环境限制，非本次改动）。
- `tests/test_fx_short_player_admission.py` 触发 1 条 `SAWarning`：GC 清理未归还连接池的 aiosqlite 连接（既有清理告警，测试通过）。
- 全套唯一 skip：`tests/test_redemption_service.py::test_purchase_concurrent_only_one_wins`，因 SQLite 不支持 `FOR UPDATE SKIP LOCKED` 真并发而按既有 `skipif` 跳过（PG 语义）；32 项 deselected 为 `-m "not pg"` 排除的 PG 文件。
- `npx eslint .` 的 44 个错误分布在 16 个**既有非 FX 文件**：`src/api/{chart,index,stream}.ts`、`src/components/home/Movers.vue`、`src/pages/admin/SiteConfig.vue`、`src/pages/auth/{Callback,Login,Register}.vue`、`src/pages/market/{Leaderboard,MarketList,TradingView}.vue`、`src/router/routes.ts`、`src/stores/{auth,market}.ts`、`src/types/{common,stream}.ts`；规则为 `@typescript-eslint/no-explicit-any` 37、`vue/multi-word-component-names` 5、`@typescript-eslint/no-unused-vars` 2。本功能改动的 8 个前端文件**均不在**该列表中，即本功能未新增 lint 错误。按约定未运行 `--fix`、未格式化或重构无关文件。
- **明确结论：这些检查不是 “all green”。** 44 个 lint error 是既有基线问题，只说明“本功能未新增 lint 错误”；`npx eslint .` 仍以 exit 1 失败，未修复、未放宽规则。

### 当前状态与最终审阅结论（2026-10-01）

- `932dc3a` 是本文验证的产品基线；whole-branch 独立审阅 H 已给出 SPEC+QUALITY PASS 且无 load-bearing 缺陷，RD 的六项 API 修复 scoped review 也通过。
- 审阅者要求的最终前端-only 修复（G 解耦 `FxOverview.vue` 的报价失败与趋势失败，报价单项失败不挡走势）已提交为 `1a373f7`，**只改该一个文件**。G 在该 delta 上报告 full front 150、type-check、build、scoped lint 全部通过；最终 H scoped review 只审该 delta，结论 **SPEC/QUALITY/READY，APPROVED**，全部代码门关闭。后端与性能被测量源码哈希在文档更新时重算，与 `932dc3a` 的测量字节逐字节一致（`source-hashes-932dc3a.txt` 与 `validation/00-recon.txt`），故不触发复跑。
- 早先把 CORS 覆盖头列为待办的 scratch note 已被取代：`backend/app/main.py` 已 `expose_headers=["X-FX-Through-Trade-ID", "X-FX-History-Version"]`，前端 `getChartWithMeta` 已消费这两个头。
- 该 delta 只改前端，后端/PG/性能被测量源码字节未变，故**不再重跑**；本文后端/PG/性能结论对 `1a373f7` 成立，前端数字以 G 在 `1a373f7` 上的报告为准。
- 审阅者更正的文档事实（见下「实现子系统映射」与 spec「实施后契约细化」）：`/chart` 为物化+ring+投影尾段（非重扫原始 `FxTrade`）；422 同时约束输出桶与细粒度来源桶；coverage 按 ring `min(windowMax, applied)` 与 DB `windowMax` 区分；SSE 首包/增量的公开尾段与 `history_invalidated` schema；新增 `/history/fx/...` immutable 段；启动为「先预热持久状态/ring，再 `catch_up`」；前端合成空桶 `v=0` 首笔成交定义真实 O/H/L/C 的不变式。

## 性能验收（V 在候选 `932dc3a` 上的复跑）

隔离 PG 上的真实异步路径（`get_public_snapshot`、`read_fx_chart_with_meta`、`read_fx_history`、真实 `FxEngine.tick`+bounded publisher+runtime owner），每点 3 次计时取中位数、≥1 次预热在计时外；cProfile 与 SQL/覆盖探针都在计时之后。完整报告见 `.superpowers/sdd/2026-10-01-fx-market-data-performance/task-6-performance-report.md`，机器可读结果见 `performance/results/summary.json` 与 `baseline.json`/`feature.json`。

| 指标（应用 CPU，ms） | 基线 `0a1e12f` | 候选 `932dc3a` | 变化 |
| --- | ---: | ---: | ---: |
| `get_public_snapshot` CPU | 274.10 | **3.48** | −98.73 % |
| `/chart` 15m×25h CPU | 565.60 | **2.29** | −99.60 % |
| 已封存 15m 段（命中缓存）CPU | — | **0.97** | — |
| tick 无订阅 CPU | 736.97 | **44.60** | −93.95 % |
| tick 3 订阅 CPU | 715.69 | 68.71 | −90.40 % |
| 六请求并发 wall / 应用 CPU | 2870.26 / 2851.04 | **65.15 / 62.79** | — |
| 六请求事件循环最大延迟 | 2508.42（诊断） | **10.12** | — |
| 启动 owner 回填 wall / CPU（单独报告） | — | 17945.00 / 15875.10 | — |

| 计划目标（原文不变） | 要求 | 实测 | 判定 |
| --- | --- | --- | --- |
| 快照应用 CPU 较基线下降 | ≥ 90 % | 98.73 % | **PASS** |
| 无人订阅 tick 应用 CPU 下降 | ≥ 90 % | 93.95 % | **PASS** |
| 25h/15m 重复历史应用 CPU 中位数 | ≤ 20 ms | 2.29 ms | **PASS** |
| 六请求事件循环最大延迟（同机隔离） | ≤ 100 ms | 10.12 ms | **PASS** |

复跑核对（满足任务 V 的全部复跑要求）：

- **fixture 符合预期**：3 个 pair、每 pair 18,000 条合成 `FxTrade`，5s 间隔共 25h，冻结窗口 `2026-09-29T22:59:55Z..2026-10-01T00:00:00Z`、tick 时刻 `2026-10-01T00:01:00Z`；金侧量=buy `input_amount`/sell `output_amount`，独立 oracle 不引用生产聚合代码。
- **原生 asyncPG**：`asyncpg 0.30.0`、`database_url=postgresql+asyncpg://...`、PG 14.24；无 SQLite、无 fake session、计时路径无 mock。
- **精确对等**：baseline/feature `/chart` 与 oracle 均 101/101 匹配；baseline 与 feature `/chart` body 101/101 匹配；封存段 5/5 匹配；`volume_24h` 各自匹配 oracle（滚动实时窗口在两进程间略有漂移，oracle 匹配才是判据）。
- **覆盖头精确**：全窗口 3 例与一个中段窗口（`through=9361`，非平凡）中 `through == 窗口内已提交最大 id`、`X-FX-History-Version` 等于持久化 generation、body 精确等于 `id <= through` 的 oracle，全部通过。
- **完整性**：tick 后每 pair 四个周期的 `sum(n_trades)` 与 `sum(gold_volume)` 均等于原始已提交成交的独立 SQL 聚合；feature 快照/图表/tick 的 `FxTrade` ORM 加载为 0（基线分别 9,307/18,000/27,942）。
- **回填单独报告**：启动回填 `wall 17.95 s / CPU 15.88 s`，之后 `history_ready=true`、`applied == durable == {18000,36000,54000}`、持久化版本 == runtime generation；这是合成 fixture 的一次性物化，不是生产 profiling，也不是生产恢复演练。

## 实现子系统映射

| 子系统 | 文件 |
| --- | --- |
| 派生表/迁移 | `backend/app/models/fx.py`（`FxCandle`、`FxMarketDataState`）、`backend/alembic/versions/2026_10_01_1200-fx_candle_storage.py` |
| 聚合与批量落库 | `backend/app/services/fx/candles.py`、`backend/app/services/fx/candle_flusher.py` |
| 增量运行时/游标 | `backend/app/services/fx/market_state.py`、`backend/app/services/fx/market_data.py` |
| 成交接入与生命周期 | `backend/app/services/fx/{trading,engine,scheduler,shorts}.py`、`backend/app/services/credit/execution.py`、`backend/app/main.py` |
| 物化读取/历史/SSE | `backend/app/services/fx/market_reads.py`、`backend/app/api/v1/fx_stream.py`、`backend/app/api/v1/history.py`、`backend/app/schemas/fx.py` |
| 回填/赛季清理 | `backend/scripts/backfill_fx_candles.py`、`backend/scripts/season_reset.py` |
| 前端 | `thccb-frontend/src/{api/fx.ts,types/fx.ts,composables/useFxCandleHistory.ts,utils/fxCandle.ts,utils/fxHistory.ts,pages/Fx.vue,components/home/FxOverview.vue,components/chart/FxCandleChart.vue}` |
| nginx | `deploy/nginx.conf`（`/api/v1/fx/stream/` 关闭缓冲+长连接） |

已实现契约细化（经审阅者更正，供 spec/plan 对齐）：

- `/chart` 的读取来源是**物化 `fx_candle` + 内存 ring + 仅投影列的未落库尾段**，不重扫原始 `FxTrade` ORM；命中 ring 时只读 `fx_market_data_state` + 窗口最大值，未命中时用一条 `LEFT OUTER JOIN` 同时取持久化 K 线与状态，再补持久游标之上的投影尾段。
- `/chart` body 保持旧结构不变，新增两个响应头 `X-FX-Through-Trade-ID`/`X-FX-History-Version`（CORS 已暴露）。`from>=to`、非法 interval、**输出桶或细粒度来源桶**超过 20,000 返回 **422**；`history_ready=false` 返回可重试 **503**。
- 覆盖游标按路径区分：**ring 路径**取 `min(对齐窗口内已提交最大 id, ring applied_trade_id)`；**持久化 DB 路径**直接取对齐窗口内已提交最大 id（原始尾段以其为上限，不额外被 durable 游标压低）。
- `/snapshot` 追加公开引导字段 `history_version`（可空 opaque）与 `history_ready`（bool）。
- SSE 首包在旧报价/新闻字段外追加 `history_version`、`history_ready`、`history_tail`（`10s/1m/15m/1h` 列式段）、`history_tail_at`、`history_tail_through_trade_id`；增量 `fx` 帧追加公开 `trades`（`{id, ts, post_price, gold_volume}`），缓冲溢出时发 `history_invalidated=true` 要求只补尾段。
- 新增 `GET /history/fx/{pair_id}/{history_version}/{interval}/{segment_epoch}.json`：列式十进制 OHLCV，immutable 缓存；未封存/非法 interval/未对齐段/失效版本 404，未就绪、flush 未完成或锁不可用 503，绝不缓存不完整段。
- 24h 精确量单条 SQL（分钟桶+两端原始补算+未落库尾段，共享 MVCC 快照）；`/history/fx/` 缓存 MISS 用短读事务 `fx_pair` 共享锁，生产者先取 `FOR UPDATE`，读后即 commit 释放。
- 启动顺序（实测）：先加载持久游标/就绪状态并从物化 K 线预热 ring，再 `catch_up` 补齐差额并 `flush_once`，之后才启动经济生产者；只读实例只预热。
- 前端不变式：真实 `FxTrade` 金侧成交量为正 ⇒ `v=0` 唯一对应本地合成空桶（`o=h=l=c=prevClose`）；该空桶收到首笔真实成交时必须用该笔 `post_price` 定义整根 O/H/L/C 并写入真实量，不能保留 `prevClose` 或错误极值。

spec 的「实施后契约细化」小节与 plan 的验证证据引用已按上述实际实现更正。

## 有意义的行为证据（复用现有测试，仅补具体缺口）

- `test_fx_candle_storage.py`：顺序敏感的纯聚合；批次与游标同事务；重复/重试批次不重复计量；就绪标记只显式变更；过期 generation 被拒；不确定提交后保留不同区间。
- `test_fx_candle_pipeline.py`：重启/翻页不双计；per-pair 而非全站游标；通知丢失后补读；同 pair 并发合并不丢量；只读实例不写派生表；不过早 `history_ready`；FX 历史编解码不丢精度。
- `test_fx_history.py`：对真实行的 `/history/fx`、`/chart`、精确滚动 24h 读取；空 ring 时走持久化 K 线防线（即只读实例路径）。
- `test_fx_market_data_integration.py`：真实提交钩子、replay/拒单/会话内回滚不产生派生数据、重建产生全新聚合并轮换 generation、尊重冻结截止 ID。
- `test_fx_migration.py::test_candle_migration_adds_derived_only_and_preserves_ledger`：只增派生结构、原账/成交保留、可回滚删派生。
- PG：`test_pg_fx_candle_storage.py`（`GREATEST`/`LEAST` upsert、真实行锁串行化重复/不确定批次）、`test_pg_fx_history_locking.py`（生产者在途未提交成交与并发缓存 MISS 读取共享锁交错，提交后因持久游标落后拒绝 503，回滚后放行空封存段）。

## 迁移、回填、启动与回退顺序

1. `alembic upgrade fx_candle_storage_20261001`：仅建 `fx_candle`、`fx_market_data_state` 与 `(pair_id, id)` 索引，不在迁移中扫描生产成交；downgrade 只删派生结构。
2. 离线回填（后端停机）：`python -m scripts.backfill_fx_candles --pair-id N [--through-trade-id T] [--yes]` 或 `--all`；先取经济写 ownership，再取 pair GATE 与 `fx_pair` 行锁，冻结 `max(fx_trade.id) <= through-trade-id`，在**同一事务**内替换派生行并轮换 generation；不改原始成交/资金/审计。
3. 新代码启动：owner 实例 `start_fx_market_data(write_owner=True)` 先加载持久游标/就绪状态并从物化 K 线预热 ring，再 `catch_up` 补齐游标与已提交来源的全部差额并 `flush_once`，**之后**才启动可产生 FX 成交的经济调度器；只读实例只预热持久状态、绝不创建派生数据。停机按“停经济生产者 → 排空行情消费并最终 flush → 停 publisher → 释放 ownership”。
4. 读取切换：`/chart`、`/history/fx/`、SSE 与前端缓存历史；未就绪的 pair 明确返回可重试非 200。
5. 回退边界：回到旧读路径，原始资金账不变。旧写程序期间派生状态会停滞，再次启用必须从可靠游标补齐并刷新历史版本，不能信任旧写程序期间的派生缓存。赛季重置在 `FX_CLEAR_ORDER` 中先清 `FxCandle`/`FxMarketDataState` 再删 pair，并在提交后 `FX_MARKET_DATA.stop()+reset()`。

## 六个任务的完成状态（以实际实现与最终证据为准）

计划里的命令、路径和接口是执行草案；下表按**实际实现**记录完成状态，不逐条回勾与实现不同的草案项。实际检查与证据（本表、V 报告、本验证文档）取代计划复选框。

| 计划任务 | 状态 | 实际实现与证据 |
| --- | --- | --- |
| Task 1 先降低快照与系统发布 CPU | 完成 | `trading.get_public_snapshot` 改为精确 SQL 聚合；bounded publisher 无订阅时跳过快照；V 复跑快照 CPU 274.10→3.48 ms |
| Task 2 派生表与可靠增量聚合 | 完成 | `FxCandle`/`FxMarketDataState` + 迁移 `fx_candle_storage_20261001`；`candles.py`、`candle_flusher.py`、`market_state.py`；PG/SQLite 存储与流水线测试 |
| Task 3 所有成交入口与恢复生命周期 | 完成 | 各事务拥有者提交后 `notify_committed`；`FX_MARKET_DATA.start/stop/reset`；`scripts/backfill_fx_candles.py`；`main.py` 启停顺序；赛季重置先清派生表 |
| Task 4 物化读取、历史缓存与 SSE | 完成 | `market_reads.py`、`fx_stream.py`、`history.py` 的 `/history/fx/...`；`schemas/fx.py` 公开引导字段；PG 历史锁测试 |
| Task 5 前端缓存历史与实时增量 | 完成 | `useFxCandleHistory.ts`、`fxCandle.ts`、`fxHistory.ts` 与三个组件；最终审查 delta `1a373f7`（仅 `FxOverview.vue`）已 APPROVED |
| Task 6 性能验收与发布准备 | 完成 | V 在 `932dc3a` 上以原生 asyncPG 复跑四目标全 PASS；本验证文档与 `docs/api.md`/`docs/fx.md` 事实更新 |

与计划草案的实际差异（以实际为准，未回改计划正文的其余草案）：

- Task 2 草案写的是全局 `FX_CANDLE_FLUSHER.flush_once()`；实际**不存在**该全局，刷盘入口是 runtime 持有的 `FX_MARKET_DATA.flush_once()`（内部 `FxCandleFlusher.flush_once()`），`FX_MARKET_DATA.flusher` 是只读句柄。计划接口行已同步更正。
- Task 3 草案提到的 `tests/pg/test_pg_fx_market_data.py` 实际拆成 `tests/pg/test_pg_fx_candle_storage.py` 与 `tests/pg/test_pg_fx_history_locking.py`。
- Task 6 草案的 `docs/fx-market-data-performance-validation-<执行日期>.md` 实际为 `docs/fx-market-data-performance-validation-2026-10-01.md`。

## 限制与未验证项

- **UI 渲染/截图未验证**：本会话无浏览器工具，且工作约定为最多截图、不自动点击/填表；未做任何浏览器交互。`nginx -t` 也未运行（该二进制在本机不存在）；未安装部署包、未触碰真实 nginx。
- **真实只读/standby 历史缓存限制**：`/history/fx/` 缓存 MISS 需要可加锁的会话（`fx_pair` 共享锁）才能证明封存段不可再变；真正 SQL 只读/standby 连接拿不到锁时返回非缓存 503，客户端回退 `/chart`，绝不写入不可变缓存。该分支已实现，且可写 PG 上的锁边界（在途生产者/回滚）已被 `test_pg_fx_history_locking.py` 覆盖，但**未**在真实只读 PG 副本上实测。分段 LRU 为进程内、不跨实例共享；只有写 owner 持有 ring，只读实例依赖持久化 K 线。
- **性能数字的边界**：合成 18,000 笔/25h fixture，不是生产数据或生产 profiling；PG 服务端 CPU 在 bwrap PID namespace 下不可见（`pg_cpu_ms=null`），应用 CPU 用 `process_time()`，wall−CPU 为可观测非 CPU 份额；主机同时有其他 worker 负载，wall 会漂移，故以应用 CPU 中位数为主。所有阈值是同机隔离的诊断性验收，**不是生产 SLO**。
- **测试硬超时未生效**：pytest-timeout 未安装，`timeout=30` 仅产生配置告警；本轮无测试挂死。
- 本验证没有推送、部署或对生产库运行回填/赛季重置（父流程在本分支已有本地 commit）；未做多实例/多副本压力测试。本轮只验证单机隔离 PG14 与一次性 SQLite。

## 结论

候选 `932dc3a` 上：后端字节码/导入、独立 FX 端到端（18）、现有后端全套（1483 通过/1 既有 SQLite skip）、独占 PG 套件（32）、前端单测（150）/类型/构建、`git diff --check` 均通过；只读 lint 的 44 个错误全在既有非 FX 文件，本功能前端文件未新增错误，但 `npx eslint .` 仍 exit 1，因此**不能说 “all green”**。V 在同一源码哈希（`source-hashes-932dc3a.txt` 与本地重算逐字节一致）上以原生 asyncPG 复跑，四个计划目标全部 PASS，对等/覆盖头/完整性证据齐备。**本记录不构成部署或上线声明；`932dc3a` 与前端 delta `1a373f7`（仅 `FxOverview.vue`）均已通过最终 H 审阅（`1a373f7` 为 SPEC/QUALITY/READY APPROVED），全部代码门关闭。真实只读 PG 副本、浏览器渲染与 nginx 仍未验证。**
