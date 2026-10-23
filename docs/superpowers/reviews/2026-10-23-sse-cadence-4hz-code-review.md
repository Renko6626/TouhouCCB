# 4Hz 有限增量审阅

- 已审 8Hz 候选：`e9c0242019cc5aecd4cbcd29ca5d27b4191cee62`
- 本次候选：`7406d9c6caabfd3501c5f1279f8e6c3db22e86ae`，已核对 worktree HEAD。
- 依据：`4hz-review.diff`、`4hz-implementation-report.md`，以及两处常量调用位置与 LMSR 公共 tick 常量。只审本次四行增量；前次完整审阅见 `code-review.md`。

**Spec 符合性：approved。Code quality：approved。未发现 P0/P1/P2 问题，无修复请求。**

公共 FX 的 `backend/app/services/fx/publisher.py:38` 和私人 pricing 的 `backend/app/services/trading_prices.py:20` 均改为 `PUBLISH_INTERVAL = 0.25`。前者仍在首项到来后等待完整窗口，后者仍以读取开始时间计算最小间隔，默认目标因此变为最多约 4Hz；算法、接口和生命周期没有变化。公共同批可处理多个 pair，不将 4Hz 误解为所有 pair 合计每秒四帧，也不承诺网络 250ms 内交付。

`backend/tests/test_fx_publisher.py:79`、`backend/tests/test_trading_stream.py:290` 两个实际开始间隔断言独立改为至少 250ms，原最新值、完整字段、尾项 wake 和通知断言保持。`backend/app/services/tick_broadcaster.py:44` 仍为 `TICK_INTERVAL = 0.125`，LMSR 公共保持 8Hz；私人 pricing 中的 LMSR 随本次全局 pricing 间隔改为 4Hz。account_changed、fx_order_result 的原即时 FIFO 旁路未变。

实现报告记载相关集中检查 exit 0、55 passed in 10.37s，`git diff --check` 通过。本审阅未重复测试、运行负载、访问生产或修改产品/工具。

已审 driver 的 current 分支读取所选 worktree 的真实实现，无需为 4Hz 新增变体。后续范围限 100 用户、rate20/54 各 40 秒及原 10 秒暖机；复用已有 baseline100/8Hz100 数据，不运行 200 用户。单轮及不同执行时间造成的漂移限制应在比较结果中保留，本审阅不预判性能数值。
