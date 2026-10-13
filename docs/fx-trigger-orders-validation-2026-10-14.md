# FX 到价挂单验证记录（2026-10-14）

隔离分支 `feat/fx-trigger-orders-20261014`，worktree `/tmp/sunyunbo/thccb-fx-trigger-orders-20261014`。Task6 检查代码为 `2751953`；本任务只改文档，没有新增生产代码、算法、恢复机制或永久测试。Tasks1–5 已逐任务 Sol medium 审阅通过；Task6 与全分支最终 medium 审阅待完成。未推送、合并、部署或访问生产数据库。

## 最后一轮集中检查

环境：Python 3.13.2、pytest 9.0.2、Node v24.21.0、npm 11.19.0、Vitest 4.1.11。没有复制 `.env`。后端以下每条命令均带 `DATABASE_URL=sqlite+aiosqlite:////tmp/sunyunbo/thccb-fx-orders-final-test.db`，使用明确指定的独立临时 SQLite 库；现有 conftest 的 `setdefault` 不会替换已明确设置的 `DATABASE_URL`。默认 pytest 配置 `-m "not pg"` 排除 PG 专项。

```bash
cd backend
DATABASE_URL=sqlite+aiosqlite:////tmp/sunyunbo/thccb-fx-orders-final-test.db venv/bin/python -m compileall -q app
DATABASE_URL=sqlite+aiosqlite:////tmp/sunyunbo/thccb-fx-orders-final-test.db venv/bin/python -c 'import app.main'
DATABASE_URL=sqlite+aiosqlite:////tmp/sunyunbo/thccb-fx-orders-final-test.db venv/bin/python -m pytest -x
```

编译和导入均 exit 0；导入提示开发环境自动生成 SECRET_KEY、未配置 Casdoor SSO。此前 SQLite fixture 在沙箱内反复挂起，本轮按已批准的本机执行权限运行隔离测试，没有再重复沙箱超时。

后端全量 exit 0：1456 passed、4 skipped、44 deselected、1 warning，269.63s。44 项为默认排除的 PG 测试；4 个跳过项不计作通过。警告来自 `test_fx_stream_chart.py` 的 SQLAlchemy 非归还 aiosqlite 连接清理提示，未发现本轮具体资金或订单失败。

```bash
npm --prefix thccb-frontend run test:unit
cd thccb-frontend
./node_modules/.bin/eslint .
```

前端全量单元检查 exit 0：23 文件、252 用例通过，864ms。只读 ESLint exit 1：40 errors、0 warnings，均为本分支未修改文件中的既有 any、组件命名或未使用变量问题；没有使用 `--fix`，没有顺手清理旧错误。`git diff --check` exit 0。

## 复用的有效证据

- Task1：既有相关检查 77 通过，最终指令检查 6 通过；网页包装器、事务内回滚及借款/跨零资金逻辑使用真实生产路径。
- Task2：最后订单与真实迁移升级/回滚检查 5 通过；同批其他迁移/reset 27 通过。升级对完整旧 User、Wallet、Execution、Trade 行作前后快照，验证原值保留；验证状态/数值/指令约束、外键以及非空挂单表拒绝有损降级。删除检查、赛季 reset 顺序和 business reset 接入已在代码及既有检查中核对；audit_id 为空的 reset 夹具限制仅代码检查，未声称另有实测。
- Task3：订单 12 通过，既有 net/short 72 通过。实际隔离 PostgreSQL 命令如下，最终 3 通过（2.13s），不是最初受沙箱限制的 3 skipped。之后跨 5 个币种的同账户并发建单修订实际 1 通过（0.72s），保留最多 3 单及现金不变断言。

```bash
cd backend
TEST_PG_DATABASE_URL=postgresql+asyncpg://sunyunbo@127.0.0.1:55547/thccb_fx_orders_test venv/bin/python -m pytest -q -rs -m pg tests/pg/test_pg_fx_trigger_orders.py
TEST_PG_DATABASE_URL=postgresql+asyncpg://sunyunbo@127.0.0.1:55547/thccb_fx_orders_test venv/bin/python -m pytest -q -rs -m pg tests/pg/test_pg_fx_trigger_orders.py::test_concurrent_creation_holds_account_limit
```

PG 是 parent 管理的一次性测试集群，证明跨币种槽位锁和撤销/成交互斥；本任务未重跑或停止该集群。SQLite 全量不代替 PG 行锁证据。

- Task4：`venv/bin/python -m pytest -q tests/test_fx_trigger_orders.py tests/test_site_config.py tests/test_site_config_api.py tests/test_trading_stream.py` 实际 37 通过（4.53s）；最后 scheduler 停机等待检查实际 1 通过（0.20s）。覆盖固定扫描观察价、暂停到价失败、永久不可用终止、配置保存/reschedule 和本人成功/失败 SSE。停机检查以同步事件协调，没有睡完整扫描周期。
- Task5：`npm --prefix thccb-frontend run test:unit -- src/stores/__tests__/tradingSession.spec.ts` 19 通过；最终 raw string 修复后 `npm --prefix thccb-frontend run type-check` 和 `npm --prefix thccb-frontend run build` 均通过，Vite 3141 modules、18.62s。修复前构建 17.94s；最终修复后证据才是当前构建依据。前端之后未改代码，本任务未重复构建。既有 empty charts chunk、auth 静动态导入和大于 500KB chunk 警告保留。
- Task5 修复 `2751953` 的临时 `node /tmp/sunyunbo/task5-raw-decimal-runtime.cjs` 实际通过：编译真实 SFC、调用真实输入与提交回调，捕获 API 请求中 trigger/amount/borrow_limit 原始十进制字符串；API 写入 mocked，没有浏览器或后端交易。解决实际 number v-model 导致 `.trim()` 抛错的问题，没有新增永久验证工程。

## 截图与交付范围

Task5 的 `task-5-desktop.png`（1280×1000）和 `task-5-mobile.png`（390×844，全页）是实际组件、实际 store、拦截代表性只读数据的隔离组件 fixture，共两张。两者无 page error、文档宽度等于 viewport；移动布局据截图修正。它们没有验证整张在线 FX 页、实际表单请求或生产账户。

没有浏览器自动点击、填表、提交、撤单或交易。失败通知由 store 单元检查覆盖；失败列表、开启借款和平仓表单的所有视觉组合没有分别截图。没有生产混合压测、完整真实登录交易流、停机期间触价追溯、离线通知补发或生产迁移验证。分页恰好满页可能再出现一张空结果页，沿用上一页返回即可，未增加额外机制。

本次迁移为 `business_baseline_20261013` 后新增 `fx_trigger_orders_20261014`。上线迁移、运行配置、开市、部署验收和生产操作需后续安排；当前证据不构成线上容量或已发布承诺。
