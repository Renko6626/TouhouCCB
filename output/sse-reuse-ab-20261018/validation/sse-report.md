# SSE 复用实施报告

## Preflight

基线 `58101e1`，分支 `perf/sse-reuse-20261018`。已读 brief、AGENTS 与本轮额外约定。

公共 `MarketEventBroker.publish` 已共享 `sse_pack(...).encode()` bytes；PricingPublisher 已对 changed group 共享 model_dump。此次只补齐私有发布的订阅子集 payload 与 compact JSON/UTF-8 帧复用：现有 queue 仍用 `(event, wire)` 二元结构，wire 改为不可变 bytes；同一发布中的相同 affected subset 构造/编码一次；account/order 通知编码一次。user_trading 消费已编码帧，保留 event 标签触发账户依赖刷新。只增加编码/入队 helper，局部每轮复用表，不增加持续缓存/队列/状态机。

生产队列消费者只有 user_trading；测试有直接读取 queue 的断言，需要适配 wire 解码。认证期限、15 秒独立检查、pending_frame 顺序、全部通知、队列 64 满关闭、公共初始 anchor/history cursor 均保留。公共 realtime/fx_stream 本轮无需改动（包括 MAX_PER_IP）。不改 writer/risk/funds，不启动服务/PG/负载，不自行委派审阅。

验证：先运行相关已有 baseline；扩展现有私有有界队列、共享定价测试和客户端流测试覆盖共享不可变 wire、缺失/变化和混合 FIFO；使用项目 venv 和明确临时 SQLite，仅运行受影响已有测试。

## 实施与自审

生产改动限于 `trading_prices.py`、`account_events.py`、`user_trading.py`。`encode_frame` 从路由搬到 account_events 并返回 UTF-8 bytes；compact JSON 的默认 ensure_ascii 与 separators 保持原样。已有 queue 容量、关闭和 FIFO 行为保持；同步、rules、heartbeat 也复用同一编码 helper，但仍逐连接在原位置产生。`offer_encoded` 只入现有队列，不设新状态。账户/订单发布无订阅者时直接返回；有订阅者时一次构造编码后按原顺序入队。PricingPublisher 的 `frames` 是每轮局部字典，仅按本轮 changed 与 wanted 的交集复用；没有跨轮缓存。

实际生产消费者核对：只有 user_trading 读取私有 Subscription.queue；所有其他生产引用均为通知生产者。既有测试共用的 private_events fixture 增加短解码适配，保持账户业务断言；直接读 queue 的 pricing、FX service/net-trading 测试同步解码。未更改 writer/risk/funds 或其他产品逻辑。

自审逐项核对：
- 事件标签仍与 wire 同时入队，account_changed 仍在输出前执行 read_dependencies；pending_frame 在 due 检查期间不出队越序。
- 原 time/deadline、request.is_disconnected、15秒检查、closed 检查位置未调整。
- 相同 affected subset 的 server_time 为该次帧构造时间；不同 subset 依原逻辑各自构造。groups、missing_groups 排序和字段不变。
- 对所有订阅者的广播同步且无 await；编码移至发布端不能使账户/订单事件插队。bytes 防止发布后 payload 修改影响待发帧。
- 公共 realtime 与 fx_stream 保持零改动；现有共享 bytes、MAX_PER_IP、FOR SHARE、snapshot anchor/history cursor 均不变。

## 验证证据

解释器固定 `/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python`；命令均在隔离树 backend 下执行，无 PG/HTTP/负载进程。

1. 基线：`DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-sse-baseline-20261018.db <python> -m pytest -q tests/test_account_events.py tests/test_trading_context.py tests/test_trading_stream.py tests/test_realtime_broker.py tests/test_fx_stream.py tests/test_stream_history_tail.py` → **41 passed in 4.48s**。
2. 扩展既有行为覆盖后、实现前：`DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-sse-red-20261018.db <python> -m pytest -q tests/test_account_events.py::test_private_bounded_independent_delivery tests/test_trading_context.py::test_pricing_publisher_shares_serialization_and_filters_dependencies` → **2 failed**：旧 queue payload 不是 bytes；相同订阅子集尚未共享帧。首次编辑脚本工作目录不匹配，未写入文件，已纠正；其后按上述命令确认失败。
3. 实现后：`DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-sse-green-20261018.db <python> -m pytest -q tests/test_account_events.py tests/test_trading_context.py tests/test_trading_stream.py tests/test_fx_trading_service.py tests/test_fx_net_trading.py` → **66 passed in 12.78s**。

仅扩展已有测试：不可变共享 wire、发布后修改输入不改待发内容、原 compact JSON 与中文 escaping、相同子集共享、不同依赖过滤、后续变更/缺失分组，以及真实 ASGI 帧的 pricing → account_changed → fx_order_result 顺序。没有新增独立永久测试或测试层。

提交前补跑受共享 fixture 影响的代表性已有业务测试与三个直接 SSE 文件，结果见后续记录。未运行全仓、PG、浏览器、HTTP服务或性能测试；独立审阅与统一性能对照由主agent执行，不能凭当前检查声称性能收益。

4. 最后一处局部自审修正：`Subscription.offer` 在预编码前保留已关闭时直接返回（避免对已关闭连接做无效编码），未改变入队 helper 的关闭检查。
5. 最终相关检查：`DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-sse-fixtures-20261018.db <python> -m pytest -q tests/test_account_events.py tests/test_trading_context.py tests/test_trading_stream.py tests/test_redemption_service.py::test_purchase_happy_path tests/test_loan_sweep.py::test_sweep_accrues_interest tests/test_credit_execution.py::test_cash_recovers_without_selling_and_event_product_nullable tests/test_loan_api.py::test_borrow_success_updates_cash_and_debt tests/test_writer_admin_ops.py::test_account_hint_follows_writer_projection tests/test_credit_liquidate_group.py::test_replay_same_run_round_does_not_sell_twice tests/test_admin_amnesty.py::test_execute_resets_cash_and_clears_debt_with_single_ledger_entry tests/test_danmuku_service.py::test_exchange_happy_path` → **23 passed in 4.51s**。
6. `git diff --check` → **exit 0**。

自审未发现需要新架构决策的问题；无待补功能、无新增防御机制。用户确认的性能实测尚待主agent执行。

## 交付

提交 `d18445b`（`perf: reuse encoded private SSE frames per publication`），范围为上述 3 个生产文件与 5 个直接相关测试文件。主agent所有的未跟踪计划文档保持原样，未stage；报告按要求保留在本任务 scratch 路径供控制器收集。未推送或部署。
