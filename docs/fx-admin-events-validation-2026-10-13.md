# 管理员资金与统一价格事件验证（2026-10-13）

代码基线为 `19ed988`，被测实现为 `8699e2d`，本记录中的金融数据和性能证据来自可丢弃 PostgreSQL；未连接生产库。金融场景的有效三轮中位数未超过排查门槛，但热点图表读取有明确退化，仍待局部优化和复测，不能据此宣布性能收尾完成。

## PostgreSQL 对照

本机 PostgreSQL 位于 `127.0.0.1:50557`；baseline 与 current 分别使用 `thccb_admin_fx_baseline_test`、`thccb_admin_fx_perf_test`，迁移与 ownership 使用另外两份隔离库。旧版本仅通过 `git archive 19ed988 backend` 提取到 `/tmp/sunyunbo-admin-fx-task6-20261013`，共用现有 venv；没有读取旧计划 scratch 文件或生产 `.env`。父 agent 完成截图并关闭 Vite/Chromium后明确 ACK，才开始 timed trials。

临时 runner 调用真实 `execute_net_trade`：普通买入 1 金圆券；跨零卖出 14 外币，先卖 10 现货再开 4 空头，两腿必须真实落账。每个版本每轮重建相同 schema 和数据，使用独立用户、同一场景共用交易池；每场景预热 8 请求后测 120 请求，并发 4。热点图表调用真实 `read_fx_chart`，使用 120 个物化分钟桶及关联成交／事件，reader-only runtime 暖启动 ring，两个版本均 ready。图表历史是性能夹具，不是历史金额恢复的验收样本。

测量包含真实 PG I/O、事务、服务锁、提交及无订阅者的提交后通知；金融延迟从取得 benchmark semaphore 后开始，包含服务 pair gate 排队，不包括 semaphore 等待。没有 ASGI、HTTP 鉴权、网络、浏览器、后台消费／定期 flush、强平扫描或线上混合负载。SQL监听和 flush计数对两个版本同等启用；SQL耗时是 cursor调用耗时，包含驱动／数据库往返，不等于 PG纯执行时间。

顺序为 baseline1/current1、current2/baseline2、baseline3/current3。表中 rps和 p95均取三个轮次值的中位数；p95为各轮120样本的排序第114项，不是三个轮次合并后的 p95。

| 场景 | baseline rps | current rps | 变化 | baseline p95 ms | current p95 ms | 变化 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 普通成交 | 90.650 | 84.127 | -7.20% | 48.626 | 49.287 | +1.36% |
| 跨零成交 | 33.451 | 31.194 | -6.75% | 134.066 | 146.418 | +9.21% |
| 热点图表 | 454.997 | 269.923 | -40.68% | 9.145 | 16.015 | +75.13% |

| 轮次 | 普通 rps / p95 ms | 跨零 rps / p95 ms | 图表 rps / p95 ms |
| --- | ---: | ---: | ---: |
| baseline-1 | 90.650 / 48.626 | 33.782 / 127.920 | 389.692 / 12.355 |
| current-1 | 81.009 / 54.183 | 30.854 / 146.418 | 236.063 / 23.381 |
| current-2 | 84.127 / 48.168 | 31.194 / 141.178 | 271.987 / 16.015 |
| baseline-2 | 105.414 / 43.717 | 33.451 / 134.066 | 481.752 / 9.145 |
| baseline-3 | 87.669 / 52.662 | 32.448 / 134.542 | 454.997 / 8.776 |
| current-3 | 91.070 / 49.287 | 31.804 / 148.713 | 269.923 / 14.910 |

## 成交写入成本与门槛调查

每个普通请求 baseline/current 均为 6 SELECT、4 UPDATE、3 flush；INSERT从4增加到5。跨零请求均为26 SELECT、8 UPDATE、4 flush；INSERT从6增加到8，即每条真实腿增加1条事件 INSERT，没有增加显式 flush。普通 INSERT往返耗时中位数从1.499增至2.149ms/请求；跨零从2.694增至3.722ms/请求。事件随原账务 flush进入同一事务；PG上需要维护主键、两个范围索引和两份来源唯一索引，新增审计 ref/type/id索引也会影响原审计 INSERT。计数不能把数据库提交或索引成本隔离归因给某一项。

最初三轮 ordinary的轮次 p95中位数增长15.42%，已触发调查；同组 chart夹具只有旧物化桶但 event cursor为0，current正确使用未暖好 ring的持久化路径，不能作为热点图表对照。原始 preliminary 数值保留在本任务 scratch，未用于有效结果表。修正来源身份／cursor后重新执行完整三个交替配对，并增加同等 SQL时间监听；有效金融组仍有吞吐下降，未越过-10%吞吐／+15% p95排查门槛。前后组数据、仪器不同，不能把重测解读为已消除原 p95风险。金融门槛只是调查触发条件，不是线上容量保证。

## 图表读取退化与待处理项

有效热点图表每请求 SELECT由3增至6，flush为0，SQL往返耗时中位数3.628→8.328ms/请求。当前 ring快路径依次查询：pair、market_data_state、窗口前 anchor event ID、窗口内 MAX(event ID)、受 applied cursor限制的 prior-close anchor价格、返回前 generation复核。旧版本为pair、state和 prior-close anchor，新增精确 coverage与generation保护增加三次往返。该成本在固定120桶且历史已ready时仍可见，不由全历史重扫引起。

最小局部优化候选：在一个投影查询中读取pair／state及窗口前anchor和窗口内 MAX(event ID)，并复用 anchor价格投影；保留 ring快照与 applied cursor比较、最终generation核对、持久化 fallback的同语句state/candles快照、尾段10,000条及输出上限。不能删除generation检查或把全局max当成窗口coverage。本轮仅识别原因并提交父 agent安排局部修复，尚未改动产品读取实现。上述图表退化仍是未决风险；后续修复须另列被测提交和热点图表复测证据。

## 历史规模、EXPLAIN和存储

在current库追加旧时间的资金事件，对同一近端2小时窗口做20次直接持久化chart读取（不启runtime）。1,120与100,120条同市场事件时，输出始终120桶，各为160条SELECT（8/请求），平均14.154与16.219ms/请求。此项是有界读取检查，非交替吞吐试验；增加历史量约89倍，没有增加返回行或SQL次数。

近端事件投影查询用 `(pair_id,created_at,id)` 索引，实际扫描119条近端事件；小／大历史分别shared buffers 5／6，执行0.182／0.169ms。正常cursor页限制1,000条，两个规模执行1.118／1.219ms，buffers18／19；该分布中优化器选择主键范围扫描并过滤pair。管理员audit keyset按 `(ref_table,ref_id,event_type,id)` 反向索引扫描，返回1／50行，buffers4／53，执行0.048／0.187ms，未扫描大量其他市场记录。精确EXPLAIN输出保留本任务临时产物。

另做一次cursor选择性检查：在100,120条pair3历史之后追加20,000条pair2事件，查询pair3最近500条且LIMIT1,000。PG仍选择主键范围扫描，返回500行并过滤20,000条其他市场事件，314 shared buffers，执行2.791ms。`(pair_id,id)`索引存在，但此分布下优化器未选择它；不能声称任何历史分布都保证按pair范围扫描。这是已观察到的未决选择性限制，交由父agent审查，不扩展压测矩阵或自行新增索引。

有效金融+chart种子末尾共504条事件；第三轮事件heap为65,536 bytes，五个事件索引合计188,416 bytes，新增审计索引40,960 bytes。追加到100,120条同市场事件（另有384条真实金融腿）后，事件heap10,182,656 bytes，五个事件索引17,547,264 bytes，审计索引9,003,008 bytes。此处为 `pg_relation_size`，不含TOAST、WAL、全数据库大小或长期VACUUM／归档影响；零量事件仍占存储，行数和索引随操作增加。

## 真实PG迁移与ownership

baseline的 `test_player_commit_wakes_runtime_and_replay_rollback_rejection_do_not_count` 在独立PG库复现 `EconomicWritesDisabled: unknown`。旧共享fixture只给SQLite取得OWNERSHIP，直接服务测试没有app lifespan。修正范围仅该integration模块：session/autouse fixture先验证库名为 `test_` 前缀或 `_test` 后缀，早于function-scoped `setup_db` 的drop/create；schema建立后真实 `OWNERSHIP.acquire(required=True)`并在fixture结束释放；保留原writer guard，无mock或绕过。最终PG integration模块6 passed，SQLite migration+integration合计17 passed。

迁移库由baseline metadata建表，保留cash/debt小数、wallet、treasury、pool、原trade/audit/candle及history_version。通过SQLAlchemy asyncpg连接的 `run_sync` 与 Alembic `Operations.context(MigrationContext.configure(conn))` 调用实际revision `upgrade()/downgrade()`：升级仅新增事件元数据并置history_ready=false；无事件时downgrade/upgrade往返保留旧值；插入实际trade事件后downgrade明确拒绝，已有事件和金额仍在。原cash/debt为123.123456/45.123456，pool100/200，wallet20/40，treasury17/29，trade1/2，审计payload保持。

现有venv没有 `psycopg2`，标准 `alembic stamp` CLI在创建同步engine时失败。本轮没有安装依赖；PG迁移证据来自实际revision经Alembic Operations执行，而非完整CLI流程。未验证生产版本标记、上线迁移或生产回滚。

## 实际命令与结果

所有临时脚本位于本计划 `.superpowers/sdd/2026-10-13-admin-fx/`，不加入永久benchmark框架。工作根为 `/data/sunyunbo/.codex/worktrees/af66/TouhouCCB`。

```bash
# baseline cwd: /tmp/sunyunbo-admin-fx-task6-20261013
DATABASE_URL=postgresql+asyncpg://sunyunbo@127.0.0.1:50557/thccb_admin_fx_baseline_test backend/venv/bin/python -m pytest backend/tests/test_fx_market_data_integration.py::test_player_commit_wakes_runtime_and_replay_rollback_rejection_do_not_count -q
# 1 failed: unknown writer ownership（预期的baseline复现）

# reviewed worktree cwd
DATABASE_URL=postgresql+asyncpg://sunyunbo@127.0.0.1:50557/thccb_admin_fx_owner_test backend/venv/bin/python -m pytest backend/tests/test_fx_market_data_integration.py -q
# 6 passed in 3.88s
backend/venv/bin/python -m pytest backend/tests/test_fx_migration.py backend/tests/test_fx_market_data_integration.py -q
# 17 passed in 3.38s
backend/venv/bin/python .superpowers/sdd/2026-10-13-admin-fx/task-6-paired.py
# 6有效配对进程，各场景120请求；结果见上表

DATABASE_URL=postgresql+asyncpg://sunyunbo@127.0.0.1:50557/thccb_admin_fx_migration_test backend/venv/bin/python .superpowers/sdd/2026-10-13-admin-fx/task-6-migration.py /tmp/sunyunbo-admin-fx-task6-20261013/backend
backend/venv/bin/python .superpowers/sdd/2026-10-13-admin-fx/task-6-migrate-verify.py backend/alembic/versions/2026_10_13_fx_pool_events.py
# 旧金额/历史保留，空事件往返，populated downgrade拒绝

DATABASE_URL=postgresql+asyncpg://sunyunbo@127.0.0.1:50557/thccb_admin_fx_perf_test backend/venv/bin/python .superpowers/sdd/2026-10-13-admin-fx/task-6-bounds.py /data/sunyunbo/.codex/worktrees/af66/TouhouCCB/backend
# 规模/窗口结果及EXPLAIN见上文
```

## UI检查与未验证范围

父 agent仅在本地只读API fixtures及合成admin登录storage下导航截图，查看markets和operations的desktop/mobile四张截图，主控件没有裁切、布局可读；pageErrors=[]、unexpectedWrites=[]。截图位于 `/tmp/sunyunbo/admin-fx-screenshots-20261013/` 的 `markets-desktop.png`、`operations-desktop.png`、`markets-mobile.png`、`operations-mobile.png`。没有自动点击、填写、提交，也没有执行真实预览或注资；截图不代表真实后台交互验收。

尚未验证线上网络、实际管理员交互、生产迁移、停机恢复、事件长期增长／归档、混合负载与全站容量。离线导入归一化及排序保留O(N)内存；磁盘staging仅为后续待办，本轮没有新增临时文件功能。

前端检查由Task5在 `thccb-frontend` 完成，当前前端未变化：`npm run type-check` exit0；`npm run test:unit -- src/utils/__tests__/fxPairSetup.spec.ts src/utils/__tests__/fx.spec.ts` 47 passed；`npm run build` exit0（20.71s），保留既有empty charts、auth混合导入和>500kB bundle警告。Task6 `git diff --check` exit0。

## 热点图表局部优化复测

本附录对应 `cec92e2` 基础上的 `market_reads.py` 查询合并改动；原Task6金融与图表结果保留，未重复金融压测。暖路径先同步复制ring与applied cursor，再用一条参数化投影SQL读取pair存在性、持久state、精确对齐窗口MAX(event ID)及窗口前最后事件的ID和价格，返回前仍单独检查generation。SQL结构在模块加载时构造一次，请求仅绑定参数；没有新增行情数据缓存。窗口／锚点任一已提交ID超过复制的cursor时仍走持久化补尾，持久state与candles同语句且 `populate_existing`、tail10,000条和bucket20,000上限不变。价格仍使用原 `event_prices` 的六位小数规则。

复用Task6真实runner，仅抽取chart seed、reader-only暖启动、8次预热和120请求／并发4阶段。baseline仍为 `19ed988` archive，current为上述改动；新隔离库为 `thccb_admin_fx_chart_baseline_test` 和 `thccb_admin_fx_chart_current_test`，每轮重建schema并取得真实OWNERSHIP。三轮顺序仍baseline1/current1、current2/baseline2、baseline3/current3。原Task6输出未覆盖；中间方案分别保存 `chart-perf-results.jsonl`、`chart-perf-2select-results.jsonl`，最终为 `chart-perf-final-results.jsonl`。

| 方案 | baseline/current rps中位数 | 变化 | baseline/current p95 ms中位数 | 变化 | current SELECT/请求 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 每次构造SQL，3次读取 | 448.627 / 328.256 | -26.83% | 9.566 / 16.516 | +72.65% | 3 |
| 每次构造SQL，2次读取 | 406.033 / 307.126 | -24.36% | 10.579 / 18.477 | +74.65% | 2 |
| 复用参数化SQL结构，最终方案 | 440.189 / 580.686 | +31.92% | 9.385 / 9.286 | -1.05% | 2 |

| 最终轮次 | rps | p95 ms |
| --- | ---: | ---: |
| baseline-1 | 441.600 | 9.319 |
| current-1 | 623.401 | 6.873 |
| current-2 | 580.686 | 9.286 |
| baseline-2 | 440.189 | 9.385 |
| baseline-3 | 347.508 | 15.522 |
| current-3 | 352.547 | 14.372 |

最终baseline/current均无flush，SELECT为3→2；SQL cursor时间中位数3.775→2.594ms/请求。临时1,000次纯构造计时得到每次构造合并statement约403.876μs，引用已构造statement约0.033μs，说明单纯减少往返不足以消除Python构造成本；该计时不含编译、驱动或数据库，不是完整请求成本分解。各轮仍有噪声，尤其第三配对；最终结果只说明该隔离暖图表场景未继续呈现原退化，不构成线上混合负载或容量保证。

在Task6已有100,120条pair3历史库检查最终合并SQL，窗口部分使用 `ix_fx_pool_event_pair_created_id` index-only scan（120行、5 shared hit、0 heap fetch），prior anchor使用同索引反向扫描（1行、4 shared hit），整条statement执行0.245ms。没有全量事件扫描。原skew cursor限制保留：仅加 `ORDER BY pair_id,id` 仍选择PK并过滤20,000条其他市场记录；等价tuple谓词虽选pair索引，buffers从317增至395、执行4.909→5.356ms，未观察到可靠改善，未改变cursor查询或索引。单次EXPLAIN时间不能推广为吞吐结论。

实际检查与复现命令（工作根同上）：

```bash
backend/venv/bin/python .superpowers/sdd/2026-10-13-admin-fx/chart-perf-final-paired.py
# 6个chart-only进程，结果见最终表；未运行金融场景
backend/venv/bin/python -m pytest backend/tests/test_fx_history.py backend/tests/test_fx_stream_chart.py backend/tests/test_fx_candle_pipeline.py backend/tests/test_fx_market_data_integration.py -q
# 61 passed in 9.30s
DATABASE_URL=postgresql+asyncpg://sunyunbo@127.0.0.1:50557/thccb_admin_fx_chart_checks_test backend/venv/bin/python -m pytest backend/tests/test_fx_history.py backend/tests/test_fx_stream_chart.py backend/tests/test_fx_market_data_integration.py -q
# 43 passed in 23.29s
# 强化原cached-checkpoint夹具后单独复核同一回归：
backend/venv/bin/python -m pytest backend/tests/test_fx_history.py::test_chart_flush_before_joined_read_refreshes_cached_checkpoint -q
# 1 passed in 0.43s
DATABASE_URL=postgresql+asyncpg://sunyunbo@127.0.0.1:50557/thccb_admin_fx_chart_checks_test backend/venv/bin/python -m pytest backend/tests/test_fx_history.py::test_chart_flush_before_joined_read_refreshes_cached_checkpoint -q
# 1 passed in 1.22s
git diff --check
# exit0
```

新增永久回归仅覆盖暖ring复制后提交的window／prior-anchor事件强制fallback、bounds读取后generation轮换拒绝旧结果；复用原history夹具及真实生产聚合。原checkpoint回归显式保留旧ORM实例，继续捕获同语句读取未刷新checkpoint导致的重复成交量。既有route fake仅补合并投影字段及execute参数签名，没有放宽断言。日志、两次EXPLAIN和临时脚本保存在本任务scratch；未运行浏览器交互、生产迁移或生产环境，其他未验证范围仍见上文。

## Review fix1：先于清表的PG URL检查

首版ownership fixture依赖 `setup_db`，库名检查实际晚于drop/create；review指出这个顺序无法保护误指向非test库。已将URL检查移到本integration模块的session/autouse fixture，并让ownership fixture显式依赖它。检查只读取engine URL，不建立连接；真实writer ownership仍在setup_db建立schema后取得，未修改全局fixture或writer guard。

基于 `d300429` 的实际复查命令：

```bash
DATABASE_URL=postgresql+asyncpg://sunyunbo@127.0.0.1:50557/thccb_admin_fx_owner_test backend/venv/bin/python -m pytest backend/tests/test_fx_market_data_integration.py -q
# exit0，6 passed in 3.98s
backend/venv/bin/python -m pytest backend/tests/test_fx_market_data_integration.py -q
# exit0，6 passed in 1.00s（默认隔离SQLite）
DATABASE_URL=postgresql+asyncpg://sunyunbo@127.0.0.1:1/thccb_guard_probe backend/venv/bin/python -m pytest backend/tests/test_fx_market_data_integration.py::test_player_commit_wakes_runtime_and_replay_rollback_rejection_do_not_count -q
# exit1，预期的setup拒绝：FX integration ownership requires a disposable test database
git diff --check
# exit0
```

非test URL检查使用未监听的端口1，在session guard报错；未进入function setup_db，没有连接或清空任何非test库。此次仅修正fixture顺序；原性能证据不变，图表局部修复的复测由父agent另行记录。
