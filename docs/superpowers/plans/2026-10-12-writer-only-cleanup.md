# Writer 与 SSE 清理计划

> **For agentic workers:** 使用 superpowers:executing-plans 直接执行；用户允许 Sol medium 阶段审阅，不新增测试。

**Goal:** 删除 LMSR 非 writer 写实现和 SSE 迁移兼容，延续性能与结构优化。

**Architecture:** 沿用现有 market writer、品种门闩、经济写所有权和事务边界。API 提交命令，writer 不可用时拒绝经济写；已结算市场只读取幂等结果。LMSR SSE 只发送 snapshot/tick/ping；broker 只接受带产品前缀的字符串 topic。

**Tech Stack:** 现有 FastAPI、SQLAlchemy、Vue、Python quant；不新增依赖或 schema。

**Spec:** 用户在本会话批准删除非 writer 路径、SSE 双发和 realtime 兼容；大文件后续处理。

## Global Constraints

- 目标分支 feat/fx-net-trading，基线 3305c11；不覆盖其既有未提交文档修改。
- 临时验证树 /tmp/sunyunbo/thccb-writer-cleanup，验证后同步回原分支工作树。
- 不新增测试；复用、调整现有行为用例，删除专属旧路径/旧兼容的用例。
- 保留历史/只读数据库读取、资金精度、风控、审计、锁与结算幂等性。
- 不拆分大文件，不修改生产配置，不部署，不自动提交或推送。

## Review Focus

- writer 停止或缺少活跃市场 state 时不得执行 DB 后备写。
- 重启前已结算市场的重复结算、关闭和恢复请求保持正确返回。
- API 鉴权读事务须在等待 writer 队列前关闭。
- 同编号 FX/LMSR topic 隔离、seq/anchor、慢消费者踢出保持正确。
- 客户端通过 tick 逐笔处理成交和结算；删除旧监听不会丢行情。

### Task 1: 删除 LMSR 非 writer 写路径

**Files:** backend/app/api/v1/market.py、services/loan_migrate.py、main.py；相关已有 API/writer 测试及 conftest.py。

- [x] 删除旧 buy/sell/resolve/close/resume 写逻辑和旧门闩装饰器分流；简化为 writer 命令入口。
- [x] writer 不可用返回 503；无 state 时仅允许读取已结算结果或给出明确拒绝。
- [x] 删除 single_writer_enabled 播种与废弃配置；保留数据库历史读取。
- [x] 调整已有测试种子/生命周期以启用真实 writer；已有 candle 用例等待真实 flusher。
- [x] 运行相关现有 writer、API、滑点、准入、结算、candle 检查。

### Task 2: 收尾 SSE 和 realtime

**Files:** backend/app/services/{market_writer,writer_ops,tick_broadcaster,realtime}.py；已有 broker/tick/namespace 测试；frontend SSE 与图表旧监听；quant SSE 旧入口及文档。

- [x] 删除 legacy_trade_events、OpOutcome.publishes 和旧事件生成/发布。
- [x] broker 仅接受规范字符串 topic，删除整数 topic 多 lane 订阅和 _TopicMap。
- [x] 前端移除旧 trade/market_status 监听与图表后备 watcher；quant 保留 tick 到策略内部事件的转换，删除旧 wire 入口。
- [x] 删除专属迁移兼容测试，更新现有 topic 参数和消费用例，不新增测试。
- [x] 运行已有相关检查及项目要求的后端全套非 PG 检查、前端类型/已有单测/所改文件 lint。

### Task 3: 审阅与交付

- [x] Sol medium 审阅实际 diff，核对 Review Focus，不修改文件。
- [x] 修复必要问题，运行受影响已有检查；只在发生新改动或失败时追加验证。
- [x] 将完整改动同步回 feat/fx-net-trading 工作树，保留既有文档修改，记录验证命令和未验证项。

## 执行记录

计划依据当前会话批准的路线，直接执行；不再请求方案确认。


### 验证结果

- 基线：现有 writer E2E、namespace、tick 检查 19 passed。首次沙箱内异步数据库初始化超时；退出沙箱、使用独立临时库后通过。
- 改动相关 writer/API/SSE/candle/slippage/gating：80 passed；后续既有 PvE、审计、回填、chart、空头准入用例调整后 48 passed。
- 最终后端命令（backend）：`DATABASE_URL=sqlite+aiosqlite:////tmp/sunyunbo/thccb-writer-full.db venv/bin/python -m pytest -q`，1421 passed、3 skipped、38 PG deselected。原始日志 `/tmp/sunyunbo/thccb-writer-full.log`。
- 后端 `py_compile` 和 `import app.main` 通过，`git diff --check` 通过。最终全套有一项 SQLAlchemy 测试连接回收 warning（test_fx_trading_in_session）；未扩展处理。
- 前端（thccb-frontend）：`npm run type-check`、`npm run test:unit`（201 passed）、`npm run build` 通过。所改 6 个文件运行 `npx --no-install eslint` 有 11 项既有 any/unused 报错；同一命令在原性能分支得到相同 11 项，不扩大本次清理范围。
- quant：已有 SSE subscriber、tick、web 检查 22 passed：`/data/sunyunbo/www/TouhouCCB/quant/.venv/bin/python -m pytest -q tests/test_sse_subscriber.py tests/test_sse_subscriber_tick.py tests/test_web.py`。
- Sol medium 独立生产 diff 审阅与后续 quant/fixture 增量审阅均无 Critical/Important。
- 没有新增测试文件；原有后端 test 函数从 1308 降为 1292。保留行为断言，旧 candle 用例使用真实 flusher。
- market.py 1652→1045 行；realtime.py 327→200 行。未做新的延迟/吞吐基准，不将删除行数作为性能验收。
- 未运行 PG 专用并发测试、浏览器交互或生产部署；保留历史/只读数据库读取和已结算幂等响应。
