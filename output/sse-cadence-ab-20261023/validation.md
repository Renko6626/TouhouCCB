# SSE 限频实现记录

状态：指定实现与测试已提交，等待主 agent 安排 Sol High 独立审阅。未 push、合并或部署。

- 工作目录：`/tmp/sunyunbo/thccb-sse-20261023/implementation`
- 分支：`perf/sse-cadence-20261023`
- baseline：`8904d1a362cfb28b19ceba53a2538e3c2cf7edff`
- commit：`e9c0242019cc5aecd4cbcd29ca5d27b4191cee62`
- commit 仅包含两生产模块与三个指定测试文件；复制来的四份未跟踪文档未加入 commit。

## 实现

公共 FX 保留空闲 `queue.get()`，拿到首项后保持 pending/queued 并等待完整 125ms 窗口；窗口结束固定 `1 + qsize` 预算，首项及其余 FIFO 依次处理。同 pair 发布中的新值沿用原 requeue，不能在同批再次处理；窗口取消用 finally 对已取出首项确认一次。保留原容量、drop、无观看者门禁、异常处理、drain/stop 行为。

私人 pricing 保留普通 wake Event 和 1s wait_for 补偿，用 run 局部上轮读取开始的 monotonic 时间计算剩余间隔，等待后 clear wake，再读取。等待中的更新进入本轮读取，读取中的 wake 留给后续轮次。账户和订单通知路径没有修改。

新增/扩展用例验证公共真实发布间隔、窗口持有首项时 drain 与取消、A/B/A 公平性与尾项、私人读取前多次更新合并及读取期间 wake、pricing 等待期间两个订单结果和 liquidation 通知通过真实认证流且隔离其他用户。扩展原公共帧测试，经真实 FxPublisher + 原 publish_pair_frame + broker 检查两个事件 ID/增量保留；沿用 FakeDB/_StubRuntime，原 overflow invalidation 用例保留。

## 实际验证

所有 pytest 均在该 worktree 的 `backend` 目录，以 `env -u DATABASE_URL` 和 `/tmp/sunyunbo/thccb-pr23-review-20261023/review-venv/bin/python` (Python 3.13.5) 运行。

基线及最终集中命令：

```bash
env -u DATABASE_URL /tmp/sunyunbo/thccb-pr23-review-20261023/review-venv/bin/python -m pytest -q tests/test_fx_publisher.py tests/test_trading_stream.py tests/test_account_events.py tests/test_fx_stream.py tests/test_tick_broadcaster.py tests/test_market_read_state.py
```

- 实施前：exit 0，50 passed in 7.88s。
- 首轮实现后：exit 1，54 passed、1 failed in 10.49s；新增公共wire断言误写 `event_id`，原协议字段为 `id`，已修正测试，未改组帧生产实现。
- 修正并加强等待边界屏障后，运行相同前缀加 `-m pytest -q tests/test_trading_stream.py tests/test_fx_stream.py`：exit 0，20 passed in 3.51s。
- 最终集中验收：exit 0，55 passed in 10.52s。
- 另外将公共生产模块临时换回 baseline（try/finally 恢复当前实现），运行 `tests/test_fx_publisher.py::test_publication_is_coalesced_per_pair_to_the_latest_price`：预期 exit 1，新增最小125ms断言失败，实际两次开始相隔约0.095ms。证明旧实现缺少该行为；最终集中验收在恢复当前实现后运行。
- `git diff --check`：exit 0，无输出。
- 自审完整差异；未修改主仓库或范围外代码。模块局部 asyncio 引用代理没有修改共享 asyncio.sleep。原 coalescing/容量断言保留。

## 检查边界和待审点

公共窗口内首项离开 queue 后仍留在 pending；window_complete 局部标志只用于保证窗口取消时 task_done 一次。请重点复核正常第一项的确认与取消分支、固定预算下 in-flight requeue、公平性和 drain idle 判定。

请复核私人 clear wake 的位置，确保等待期间更新进入本轮、读取期间更新保留到下一轮。新增流用例使用真实 registry/DB 读取与 request_stream，屏障只控制模块局部等待或真实读取完成后的返回边界。

测试使用 PID 独立 SQLite；未接触外部 DATABASE_URL 或生产环境。证据限调度、原消息语义及保留原增量组帧接口/字段；不声称验证 PostgreSQL 锁语义、真实 runtime 512溢出、真实跨零全部执行腿端到端或新的资金行为。既有金融与 tick 用例保持。

没有本次生产性能、大压测、真实代理/公网、全部订阅者逐条可靠交付、崩溃注入证据。慢 IO 仍可能串行阻塞同批其他 pair。通知沿用 best-effort，不承诺必达；未引用历史 +54.8% 作为本次收益承诺。loadtest 和后续报告由主 agent 负责，此工作不触碰实验入口。无 ASGI、SQL、缓存、资金、前端、部署或其他架构扩张。
