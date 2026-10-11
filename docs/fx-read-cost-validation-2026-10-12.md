# FX 读取精简验证记录（2026-10-12）

Tasks 0–4 完成，各阶段及最终 medium 审阅 spec/quality PASS、整体 Ready（证据目录 `final-review.md`）。Task 4 仅整合已有证据和检查文档差异；root 已关闭本任务临时 PG。下文保留当时未提交的阶段验证记录；阶段性提交与推送归档说明见文末。

工作树 `/data/sunyunbo/.codex/worktrees/2781/TouhouCCB`，基线 `29c1250d69400df6034d6850e5259b0236fcc109`。阶段报告、审阅及 Task 3 原始日志位于 `.superpowers/sdd/2026-10-12-fx-read-cost/`（下文记作证据目录）。

## 输入与计数口径

真实 FastAPI HTTP、认证、async SQL session 和生产服务；SQLite 不启动 lifespan/jobs，不运行浏览器。UTC 固定为 `2026-10-12T00:00:00Z`。每次测量前 drop/create/seed；建表、seed 和取得 economic_version 的预备报价不计入。配置冷相在预备报价后 clear_cache，热相沿用缓存；不表示数据库页缓存或启动冷热。

小账户 fixture：现金1000，池金币/外币各1000，钱包外币100/成本80，金库金币1000/外币10000，旧上限10000。纯回补/买跨零钱包为0，目标短债本金/锁金/卖出收入各20；金债100且利息时钟固定；其他品种短债使用第二池20/20/20。无历史成交/回执。买10、卖10、买跨零40、卖跨零120。mixed_debt 另有一个 TRADING LMSR 市场（b100，A/B 总份额10/0），本人A仓10/成本5。缺钱包/金库场景省略相应行。

SELECT 和 DML 是 SQL statement 次数；DML=INSERT+UPDATE+DELETE。所有 HTTP 数字含 auth。原基线证明 SQLite 采样，却不能证明当时未读取 dotenv；旧无证声明已撤回。修复后的临时 harness 独占新建限定 `/tmp` 文件，覆盖继承 DATABASE_URL，生产 import 前禁用两种 dotenv 源，import/ownership/建表前校验 engine URL。Task 1–3 使用修后 harness；不倒推旧进程隔离。

## 固定样本前后计数

冷/热列分别对应配置缓存，DML 列为前后不变值。末列明确最后实测阶段；没有把未采样场景外推到最终代码。

| 请求 | 冷 SELECT 前→后 | 热 SELECT 前→后 | DML | 后值证据阶段 |
| --- | ---: | ---: | ---: | --- |
| cash quote | 8→5 | 6→3 | 0 | Task 3 |
| mixed_debt quote | 21→18 | 18→15 | 0 | Task 1 |
| missing_wallet quote | 8→5 | 6→3 | 0 | Task 1 |
| missing_treasury quote | 8→5 | 6→3 | 0 | Task 1 |
| pure_cover quote | 8→5 | 6→3 | 0 | Task 2 |
| gold_debt quote（FX-only） | 18→15 | 15→12 | 0 | Task 2 |
| cross_sell quote | 21→17 | 16→12 | 0 | Task 3 |
| cash execute | 10→9 | 8→7 | 8 | Task 3 |
| gold_debt execute | 29→27 | 27→25 | 8 | Task 2 |
| pure_cover execute | 24→22 | 23→21 | 8 | Task 2 |
| cross_sell execute | 32→29 | 28→25 | 14 | Task 3 |
| receipt replay / parameter conflict | — | 4→4 | 0 | Task 3 |
| legacy-only conflict（独立探针） | — | 5→4 | 0 | Task 2 |

cross_sell quote 热16→12由基础读取减少3、上限 SUM 减少1组成；execute 热28→25由首次/重试查重各减少1、上限 SUM 减少1组成。cash execute 少1；有债执行保留两轮依赖发现，不能再重复计算 Task 1 报价收益。

原基线38条及 mixed 补充2条分别为 `/tmp/thccb-fx-read-cost-baseline.json`、`/tmp/thccb-fx-read-cost-mixed-baseline.json`。Task 1 全响应20行比较、Task 2 全响应18行比较和 legacy 7行比较均保留完整 JSON/status/DML；Task 3 对 Task 2 的10行全部相等，没有剔除字段。上限10000的等价样本不能证明新零上限行为；新规则由真实开仓测试单独验证。

原始当前样本为 `/tmp/thccb-fx-read-cost-task1-20261012.json`、`/tmp/thccb-fx-read-cost-task2-20261012.json` 和证据目录 `task-3-probe.json`；对比文件为对应 `/tmp/...task1-comparison-20261012.json`、`/tmp/...task2-comparison-20261012.json` 与证据目录 `task-3-query-comparison.json`。旧入口探针为 `/tmp/thccb-fx-read-cost-task2-legacy-{before,after}-20261012.json`。

## 已执行命令与结果

以下是阶段已有执行记录，本次没有重跑。backend 命令 cwd 为工作树 `backend`，frontend 命令 cwd 为 `thccb-frontend`。Task 0–2 的完整命令和执行者报告见证据目录 `task-0-report.md` 至 `task-2-report.md`；Task 3 原始日志及哈希清单供审阅。

```bash
# Task 1 backend
DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-fx-read-cost-quote-20261012.db venv/bin/python -m pytest -q tests/test_fx_net_trading.py tests/test_fx_api.py tests/test_fx_amm.py tests/test_credit_risk.py
# Task 2 related / PG（同一命令分别指定 marker）
DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-fx-read-cost-task2-tests-20261012.db TEST_PG_DATABASE_URL=postgresql+asyncpg://sunyunbo@127.0.0.1:55542/thccb_fx_read_cost_test venv/bin/python -m pytest -q tests/test_fx_net_trading.py tests/test_fx_api.py tests/pg/test_pg_fx_short_concurrency.py -m 'not pg'
DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-fx-read-cost-task2-pg-20261012.db TEST_PG_DATABASE_URL=postgresql+asyncpg://sunyunbo@127.0.0.1:55542/thccb_fx_read_cost_test venv/bin/python -m pytest -q tests/pg/test_pg_fx_short_concurrency.py -m pg
# Task 3 final related / focused correction / PG
DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-fx-read-cost-task3-final-20261012.db venv/bin/python -m pytest -q tests/test_fx_net_trading.py tests/test_fx_api.py tests/test_fx_short_trading.py tests/test_fx_short_api.py tests/test_fx_amm.py tests/test_credit_risk.py tests/test_fx_audit_replay.py tests/test_fx_short_audit_replay.py
DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-fx-read-cost-task3-fixed-20261012.db venv/bin/python -m pytest -q tests/test_fx_short_trading.py -k retired
DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-fx-read-cost-task3-pg-20261012.db TEST_PG_DATABASE_URL=postgresql+asyncpg://sunyunbo@127.0.0.1:55542/thccb_fx_read_cost_test venv/bin/python -m pytest -q -m pg tests/pg/test_pg_fx_short_concurrency.py
# Task 3 frontend
npm run test:unit
npm run type-check
npx eslint src/pages/admin/FxManage.vue
npm run build
```

- Task 1：91 passed /13.60s；Task 2：33 passed、14 deselected /5.18s，另跑 PG 14 passed /9.94s，无skip。Task 2 结果为执行者记录，未另留 pytest 原始转录。
- Task 3 `task-3-backend.log`：155 passed、2 failed /25.28s。两项新测试误读回执属性；改为通过 trade_id 读取实际成交，生产逻辑未改、期望未放宽。`task-3-backend-fixed.log`：2 passed、33 deselected /0.59s。这是两次组合证据，没有声称157项一次全绿或修后全组重跑。
- `task-3-pg.log`：15 passed /10.88s，无skip/deselection。使用 root 创建的 PostgreSQL 14.24 独立库。新增真实 net/旧入口两用户库存竞争：库存100，各借80，一成功一库存失败；最终库存20/本金80/一笔成交及审计，失败用户财务状态和记录不变。
- 前端原始日志：16 files/201 tests passed；type-check通过；build通过20.23s，有既有大chunk警告。最初 scoped lint只因既有未使用 `snapString` 失败。root 授权局部删除后 scoped lint/type-check 均exit0（`*-lint-fixed.log`、`*-type-fixed.log`）；unit/build在删除之前通过，因无调用者未重复运行。空 lint日志的exit0来自执行者报告。

最后一次采样命令（backend cwd，exit0，证据目录 `task-3-probe.log/json`）：

```bash
PYTHONPATH=. venv/bin/python /tmp/thccb-fx-read-cost-task0.py --db /tmp/thccb-fx-read-cost-task3-probe-20261012.db --output /tmp/thccb-fx-read-cost-task3-probe-20261012.json --scenarios cash,cross_sell
```

## 保留边界与未验证项

报价仍读取全部本人短债；有债交易保留锁外发现、锁内完整重发现、权威利率/风险读取、品种/User锁序及门闩。继续共用 `_write_spot_ledger`、`_write_open_ledger`、`_write_cover_ledger`，下层只flush；跨零各腿一笔原子提交或回滚。AMM、利息、手续费、账务和回放语义未重写。

旧 `short_lending_limit_foreign` 列、值和管理员 API 格式/范围校验保留，无迁移；管理页移除该设置。旧值0不再禁止新开仓，须使用现有开空开关/品种状态；实际库存和个人风险仍约束开仓。

未实施锁内依赖输入复用、前端预览/请求时机改变、公开储备/费率字段、新依赖或新模块边界；后续候选须另行确认。未验证 PG EXPLAIN/JOIN生产耗时、生产timing/吞吐、浏览器页面或交互、生产数据迁移及部署。SQLite计数不能证明吞吐更快。若未来 PG 计划或采样显示 JOIN 放大行量/耗时，应保留证据并停止扩大范围。

Task 4 文档检查命令为仓库根目录 `git diff --check`，以及对未跟踪交付文档逐个运行 `git diff --no-index --check /dev/null <path>`；结果见 `task-4-report.md`。最终审阅 PASS，root 已关闭临时 PG；交付隔离工作树，未 commit/push/deploy。


root 已执行停机命令（仅本任务端口55542的自有临时集群，保留 data/log）：

```bash
/usr/lib/postgresql/14/bin/pg_ctl -D /tmp/sunyunbo/thccb-fx-read-cost-pg-20261012/data stop -m fast
```

root 的 `exec_command` 结果为 exit_code0，输出 `waiting for server to shut down.... done`、`server stopped`。这是 root 提供的实际停机事实；本文未自行重跑。

## 后续吞吐实测

2026-10-12已补充隔离PostgreSQL的旧／新ASGI对照，72轮、32,417次成功请求、0错误；普通报价收益明显，成交未证明稳定提升。完整结果与测量限制见 [FX吞吐对照记录](fx-throughput-validation-2026-10-12.md)。这不改变上述生产吞吐及PG EXPLAIN未验证的边界。新临时集群端口55543也已关闭。

## 阶段性提交归档

本阶段代码、相关测试、设计／实施计划和两份验证记录共同归档到 `feat/fx-net-trading`。本次交付包含报价基础读取合并、成交请求身份查询合并、取消合计借币上限及管理页设置；实际库存、个人风险、锁序、原子成交与请求回放继续保留。限价单和锁内依赖复用尚未实施。提交标识以包含本文的 Git 提交为准。

提交前核对7个生产／测试／前端文件 SHA256，均与最终审阅和既有验证使用的版本一致。远端功能分支已fetch，提交前与HEAD一致。为补齐此前两项测试修正后的全组证据，在工作树 `backend` 执行：

```bash
DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-fx-read-cost-checkpoint-20261012.db venv/bin/python -m pytest -q tests/test_fx_net_trading.py tests/test_fx_api.py tests/test_fx_short_trading.py tests/test_fx_short_api.py tests/test_fx_amm.py tests/test_credit_risk.py tests/test_fx_audit_replay.py tests/test_fx_short_audit_replay.py
```

结果exit0，157 passed /25.68s。这是最新全组结果；保留上文历史155通过＋2失败、修正后2通过的记录。PG并发检查、前端检查和吞吐矩阵复用前述已验证且源码哈希一致的证据，本次未机械重跑。
