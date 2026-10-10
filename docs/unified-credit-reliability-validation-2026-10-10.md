# 统一信贷可靠性验证（2026-10-10）

本轮完成[计划](superpowers/plans/2026-10-10-unified-credit-reliability.md)中的提交结果、账户刷新、冻结原因、权威风险等级、空头估值复用、扫描统计和运行文档修复。主体产品代码范围为 `719bcb4..684fc8a`，最终补修为 `a1a778e`，运行文档提交为 `afa4ea6` 和 `121ddfb`，分支为 `fix/unified-credit-reliability`。本文记录本地验证，不替代[既有发布验收](unified-credit-risk-validation-2026-09-30.md)。

没有新增表、数据库字段、迁移或产品依赖；贷款和 LMSR 请求编号/重试去重仍[延期](superpowers/specs/2026-10-10-trade-request-idempotency-deferred.md)。没有执行部署、生产数据库操作、离线改息或生产开关变更。

## 集中检查

| 检查 | 实际结果 |
| --- | --- |
| 后端完整默认回归 | 1419 passed，1 skipped，28 deselected，233.20 秒；PG 由默认配置排除 |
| 独立 PostgreSQL 选定信用并发/强平/兼容性回归 | 9 passed，16.44 秒；包含百人混合扫描、并发 FX 买入和 tick |
| 前端完整单测 | 最终审查修复后 13 文件，135 passed |
| 前端类型检查与生产构建 | 均 exit 0 |
| 主体 11 个前端 TS/Vue 文件及补修 4 个文件分次只读 ESLint（合计 12 个不同文件） | exit 0 |
| 全项目只读 ESLint 基线比较 | 原版和本轮均为 44 errors、0 warnings；按相对路径、规则、消息和严重程度对比，无新增或删除 |
| 后端编译与应用导入 | 115 个 app Python 文件编译成功，app.main 导入成功 |
| 差异和数据库结构 | git diff --check 通过；基线/本轮合成 SQLite 的 110 个 schema 对象名称与 SQL 完全一致 |

实际命令（工作目录按注释）：

```bash
# backend
DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-credit-final-tests.db venv/bin/python -m pytest -q
DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-credit-pg-parent-tests.db TEST_PG_DATABASE_URL=postgresql+asyncpg://sunyunbo@127.0.0.1:55461/thccb_credit_test_20261010 venv/bin/python -m pytest -q -m pg tests/pg/test_pg_credit_admission_contention.py tests/pg/test_pg_credit_liquidation.py tests/pg/test_credit_pg.py
# thccb-frontend
npm run test:unit && npm run type-check && npm run build
node node_modules/eslint/bin/eslint.js . --format json
```

触及文件 lint 使用 `git diff --name-only 719bcb4 HEAD` 筛选前端 `.ts/.vue` 文件后传给上述 ESLint，无 `--fix`。全项目 lint exit 1 源于 44 项既有错误，未修改无关代码。后端编译用 `py_compile.compile(..., doraise=True)` 遍历 `app/**/*.py`，导入使用独立 `/tmp/thccb-credit-import-final.db`。

后端完整回归出现一条 SQLAlchemy 连接回收警告，位于 `test_foreign_only_short_blocks_legacy_lmsr_buy`；没有失败。测试环境原先缺失已在 requirements 声明的 pytest-timeout 2.3.1，使用 `uv pip install --python backend/venv/bin/python pytest-timeout==2.3.1` 补齐，未改依赖文件，最终无未知 timeout 配置警告。开发应用导入/测量提示缺 SECRET_KEY 和 Casdoor 配置；使用隔离合成环境。构建提示空 charts chunk、auth 静态/动态导入重合及大 chunk，未阻断构建。

## 账户查询与操作测量

控制器用同一临时脚本比较基线 `719bcb4` 与本轮代码。SQLite 独立合成库，直接调用真实异步 API handler，不包含 HTTP、认证网络或生产流量。三个账户现金 100000、金债 1000，分别有 0/1/3 个空头；每仓本金 10、利息 1、锁金和所得基准各 50；各 pair 储备相同，利率为 0，统一杠杆 20、维持率 .04。每个读取路径预热两次后测 30 次，配置缓存预热；SQL 次数不包含 handler 前认证用户加载。

第一次本轮查询测量与 PG 扫描短暂重叠；SQL 次数相同，但耗时不用于下表。为避免该负载影响，PG 扫描结束后独立重测查询，以下是该次结果。6 组读取的现金、债务、可用/锁定现金、K、风险基数、两种净值、风险等级与基线逐项一致。空头明细实际比较的 10 个字段为 `pair_id`、`principal_foreign`、`interest_foreign`、`pending_short_debt`、`restricted_gold`、`reference_cover_cost`、`reference_cover_fee`、`executable`、`risk_status`、`blocked_reason`；未测量 `currency_code`、`proceeds_basis_gold` 和 `interest_last_accrued_at`。

| 路径 | 空头数 | SQL 基线→本轮 | 中位数 ms 基线→本轮 | 本轮样本 P95 ms |
| --- | ---: | ---: | ---: | ---: |
| quota | 0 | 7 → 6 | 5.854 → 4.412 | 5.111 |
| summary | 0 | 11 → 11 | 7.765 → 7.307 | 7.482 |
| quota | 1 | 9 → 6 | 7.170 → 4.451 | 4.525 |
| summary | 1 | 13 → 11 | 9.099 → 8.689 | 9.116 |
| quota | 3 | 13 → 6 | 9.298 → 5.505 | 6.078 |
| summary | 3 | 17 → 11 | 11.297 → 8.681 | 10.080 |

额度 SQL 固定为 6，账户摘要固定为 11，不再随空头数增加。费用、含息欠币与覆盖成本没有变化。这是固定合成样本的读取测量，表中的样本 P95 不代表生产 P95。

每个账户另执行一次借 10、还 10，记录真实生产 handler 的 SQL；每种操作仅一个样本，不报告延迟分位数。

| 操作 | 空头数 | SQL 基线→本轮 |
| --- | ---: | ---: |
| borrow | 0 | 19 → 12 |
| repay | 0 | 15 → 7 |
| borrow | 1 | 23 → 14 |
| repay | 1 | 17 → 7 |
| borrow | 3 | 27 → 14 |
| repay | 3 | 21 → 7 |

6 次操作响应的资金结果与基线一致：借款后 cash=100010、debt=1010；还款后 cash=100000、debt=1000、effective=10。借款原有 effective 为 null，未更改该契约。借还款不再为操作响应进行提交后的完整估值查询；还款响应 max_borrow 允许 null，当前额度由前端随后一次 GET 获取。

## PostgreSQL 百人扫描对照

控制器独立临时 PostgreSQL 14，仅监听 127.0.0.1:55461，可丢弃数据库 `thccb_credit_test_20261010`。复用已有 PG 种子：100 个候选、3 FX + 1 LMSR；调用真实 sweep、writer 和事务所有权。每版先预热一轮，再测三轮；每轮重建相同合成数据，未连续测量已强平过的账户。

| 版本 | 预热秒 | 三轮秒 | 中位数秒 |
| --- | ---: | --- | ---: |
| 基线 719bcb4 | 5.243 | 5.129 / 5.582 / 5.626 | 5.582 |
| 本轮 684fc8a | 5.010 | 5.620 / 5.537 / 5.366 | 5.537 |

两版所有六轮测量均扫描/触发 100 人，真实执行 100 个资金动作、产生 100 个公开事件，FX/LMSR 各 50；errors=0、deadlocks=0。每轮总现金 10000.000000、总金债 165854.134971，跨版本完全一致。本轮三轮 valuation_duration_ms 为 62/62/53；警戒、阻塞和重试耗尽人数均为 0，这些分支由针对性行为回归验证。

扫描中位数约 5.582→5.537 秒，处于样本波动范围，不能据此宣称强平提速。此次优化的明确收益是账户读取及操作响应的查询减少。执行统计是包含等待的端到端时间，三 worker 的 execution_duration_ms 累加值不能当成墙钟时间或锁持有时间。

此测量未施加玩家 HTTP 流量；并发 FX 交易和 tick 的行为覆盖来自前述独立 PG 回归。没有生产规模切换、登录/行情网络、PvE/系统干预混合压测或多轮生产 P95 验收。

## 测量命令与审阅范围

从仓库根目录执行；基线 backend 为 `/data/sunyunbo/.codex/worktrees/29ab/TouhouCCB/backend`，本轮为 `/data/sunyunbo/www/TouhouCCB/backend`：

```bash
DATABASE_URL=sqlite+aiosqlite:////tmp/sunyunbo-credit-perf-baseline-20261010.db PYTHONDONTWRITEBYTECODE=1 backend/venv/bin/python /tmp/sunyunbo-credit-query-perf-20261010.py --backend /data/sunyunbo/.codex/worktrees/29ab/TouhouCCB/backend --output /tmp/sunyunbo-credit-query-baseline-20261010.json
DATABASE_URL=sqlite+aiosqlite:////tmp/sunyunbo-credit-perf-final-sequential-20261010.db PYTHONDONTWRITEBYTECODE=1 backend/venv/bin/python /tmp/sunyunbo-credit-query-perf-20261010.py --backend /data/sunyunbo/www/TouhouCCB/backend --output /tmp/sunyunbo-credit-query-final-sequential-20261010.json
DATABASE_URL=postgresql+asyncpg://sunyunbo@127.0.0.1:55461/thccb_credit_test_20261010 PYTHONDONTWRITEBYTECODE=1 backend/venv/bin/python /tmp/sunyunbo-credit-scan-perf-20261010.py --backend /data/sunyunbo/.codex/worktrees/29ab/TouhouCCB/backend --output /tmp/sunyunbo-credit-scan-baseline-20261010.json
DATABASE_URL=postgresql+asyncpg://sunyunbo@127.0.0.1:55461/thccb_credit_test_20261010 PYTHONDONTWRITEBYTECODE=1 backend/venv/bin/python /tmp/sunyunbo-credit-scan-perf-20261010.py --backend /data/sunyunbo/www/TouhouCCB/backend --output /tmp/sunyunbo-credit-scan-final-20261010.json
```

脚本和原始 JSON 留在 `/tmp`，不作为永久测试框架；关键数值在本文保存。查询脚本 SHA256 为 `6ac8a66b90efaac6c7380d100d4df4e3456b9713adde931d8de97183fbd4292f`，扫描脚本为 `c9b85a2373f0859fe9b79f9dc30b9a44ef6212766dba8d06b93711ad7a4ee733`。脚本只接受上述隔离数据库，扫描重建不触及生产数据。

任务 1–6 已分别由 GPT-6.1 Sol medium 独立审阅，未留下需修问题；实现使用用户指定的 low 强度。最终跨任务审阅检查完整变更，发现两项结果提示/刷新时序问题及两项日志/证据表述问题；`a1a778e` 修复后由同一 medium 审阅者限定范围复核，四项均解决，可完成收尾。未运行浏览器交互流程，未推送或部署。

## 最终审查修复验证

修复代码提交为 `a1a778e`。上表后端完整回归、PG 与性能测量取自修复前；本次只重跑以下聚焦检查。

本轮修复将借款/还款成功结果立即交给页面，单次额度 GET 在后台继续；刷新期间旧额度清空且保持 loading，延迟失败单独显示。无响应 Network Error 和请求超时使用结果未知的核对提示，明确 HTTP 拒绝保留服务端文案。初始估值阻塞新增每用户 phase/status/reason 诊断日志，计数与资金逻辑未改。

前端 `npm run test:unit` 最终 135 passed（13 文件），`npm run type-check`、`npm run build` 与 `node node_modules/eslint/bin/eslint.js src/stores/loan.ts src/pages/loan/Loan.vue src/stores/__tests__/loan.spec.ts src/utils/errors.ts` 均 exit 0。首次单元测试运行有 4 个新增用例因仅等待 nextTick 而过早断言失败；改为等待真实可观察状态后通过。构建保留空 charts chunk、auth 导入重合和大 chunk 提示。后端 `DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-credit-final-fix-tests.db venv/bin/python -m pytest -q tests/test_credit_sweep.py` 为 5 passed，`venv/bin/python -m py_compile app/services/credit/sweep.py` exit 0。提交前 `git diff --check` exit 0。未重跑后端完整套件、PG、性能测量，也未执行浏览器交互或部署。


## 执行中的取舍

以下为控制器记录的全部执行取舍，按发生顺序保留：

1. 旧沙箱使托管 worktree 不可写，改用当前工作目录的专用分支；代价是没有独立的物理实施目录，因此只暂存指定文件并保留四份无关未跟踪文档。切换 full access 后不再使用提权参数。
2. 按用户最新指示采用 low 实现、medium 审阅，取代旧计划的顺序内联及讨论暂停文字；请求身份基础设施继续延期，代价是手动重试仍可成为第二次操作。
3. 每任务聚焦验证，末尾集中运行必要回归和性能对照，遵守用户测试范围要求；代价是广泛回归问题集中在最终检查发现。
4. 原计划的零欠币却有锁金/所得基准与现有数据库约束冲突，改验合法空头债务账户和完整零清仓状态，保留 schema；代价是不增加绕过数据库约束的坏行覆盖。
5. FX 报价类型实际定义在 types/fx.ts，直接给该既有类型增加已批准的 margin_status，避免造额外类型接口；代价是多修改一个既有类型文件。
6. 既有 PG 扫描测试的证据输出改用 tmp_path，避免覆盖另一工作包历史材料，保留所有行为断言；代价是该测试 JSON 为临时文件，正式性能结果由本文保存。
7. 按用户已授权的提交指示保留分支并提交，跳过集成选择菜单；代价是远程推送、合并和部署不在本次交付范围。
8. 原版全项目 44 项 lint 错误保持，比较错误内容并检查触及文件，避免扩展到无关修复；代价是全项目 lint 仍 exit 1，局部检查通过不能解释成全项目 lint 通过。

最终审阅的范围边界：不承诺原请求结果重放、任意崩溃/应答丢失恢复、共享认证刷新重构、跨账号导航通知验收、绕过 DB 约束的坏行行为、任意配置和所有并发交错的穷举审计或外部日志平台验收。账号快照保护、已测资金行为和查询结果有上述证据；未运行 UI 交互和生产验收，不将它们计入通过结论。最终修复复核没有遗留同项问题。
