# SSE 限频独立审阅

- 基线：`8904d1a362cfb28b19ceba53a2538e3c2cf7edff`
- 候选：`e9c0242019cc5aecd4cbcd29ca5d27b4191cee62`
- 审阅依据：固定 `review.diff`、`implementation-report.md`、批准后的 `implementation-brief.md`、worktree 中的 `2026-10-23-sse-cadence-design.md`，以及相关生产模块、现有负载工具调用链。
- 范围：只读审阅；未改产品或工具、未重跑测试/负载、未访问生产。唯一产物为本文。

## 结论

**Spec 符合性：approved。Code quality：approved。未发现需要修复的 P0/P1/P2 问题。压测工具没有阻塞本轮 A/B 的具体错误。**

本结论允许按已授权的 100/200 用户 × 1/20/54 总到达率、每格 40 秒加原 10 秒暖机执行单轮方向性对照；不表示已获得性能收益或生产验收证据。

## 产品代码

### 公共 FX

`backend/app/services/fx/publisher.py:202–246` 符合批准算法：

- 空闲仍阻塞于 `queue.get()`；取得首项才开启完整 125ms 窗口。窗口中没有提前 pop 首项的 pending，queued 也保留，因此同 pair 更新会合并进本轮最新 publication。
- 窗口完成后固定 `1 + queue.qsize()` 预算。只有此 worker 消费 queue；原批次剩余 ID 始终排在新入队和 in-flight requeue 前面，因此预算内不会重复处理同一 pair。没有逐 pair sleep。
- 发布中同 pair 更新仍在 pending 中，原 requeue 把它放到队尾，下一批完整窗口后发布最终值。批次结束只在 queue 和 pending 同时为空时置 idle；窗口持有首项与发布期间不会提前完成 drain。
- 窗口取消时 `finally` 只确认已取出的首项；正常窗口不在此确认，而由原逐项 `finally` 确认。取消发布时仍由逐项 `finally` 确认一次。未引入首项重复 `task_done()`。
- enqueue 容量/drop、无观看者门禁、异常日志、stop 拒收后继续 drain 的逻辑保持；原组帧调用及完整 events/invalidation、共享锁、精确 volume 路径没有修改。

### 私人 pricing

`backend/app/services/trading_prices.py:266–316` 保留原 wake Event 与 1 秒补偿，在昂贵读取前按 monotonic 计算距上轮读取开始的剩余 125ms。首轮没有额外等待；等待后 clear，再无 await 地记录时间和准备本轮 keys，随后读取。等待中的 wake 被本轮最新读取覆盖；读取开始后的 wake 保留给下一轮，尾部更新不会仅因没有后续提示而被新代码清掉。

原 registry 数据来源、健康 owner 内存复用、hash 判变化和按 subset 编码复用均保持。account_changed、fx_order_result、认证/截止、sync、15 秒复核、满队列关闭、断连及 LMSR 公共 tick 均未改动。

### 测试与可维护性

差异限于两生产模块、三个指定测试文件。新增测试复用原 hook、seed/request_stream、FakeDB/_StubRuntime 和真实 broker；断言实际发布价、A/B/A 次序、drain 结果、完整 pricing 字段、真实认证流中的通知 FIFO/用户隔离及事件 ID/增量字段，不只是 mock 次数或源码结构。模块局部 asyncio 引用代理没有改写全局 sleep。两处常量和 run 局部状态足够表达本次行为，没有引入额外队列、deadline 表、锁或新接口。

实现报告记载集中检查 55 passed、`git diff --check` 通过，并记载旧公共实现会使新增最小间隔断言失败；本审阅没有重复运行这些检查。公共多事件用例证据仍限于原组帧接口/字段，不扩写为真实 runtime 溢出、PostgreSQL 锁或跨零资金端到端证明。

## A/B 工具

审阅了 `output/sse-cadence-ab-20261023/{driver.py,sampler.py,summarize.py,tooling-report.md}`，并沿调用链检查所选 worktree 的 `loadtest/fx_capacity.py`、`fx_capacity_server.py`、`fx_http.py` 与复用的 resource aggregator。

- `driver.py:32–99` 从各自 worktree 导入原 CapacityRunner/Runner，实际 seed 与 client token 数量均使用 100/200。三个 HTTP 池规则两边一致；SSE 池均为 400，避免 200 用户的 400 条流被旧池上限截断。暖机、角色准备、到达调度、报价/交易/刷新/polling 都沿用原工具。
- `driver.py:184–248` 仅在指定本机 Unix socket 上 CREATE 新的 `fxcapacity_*_test` DB；不复用或清空未知数据库。应用 URL 为私有 UDS，DATABASE_URL 显式指向新 DB；finally 清除 token/private 目录并结束应用进程。采样器明确只接受 current，所复用 wrapper 的 current 分支不启用旧 cadence、ASGI 或 split-read 变体。
- `driver.py:111–169` 把实际用户数用于初始 gold/cash 期望，并保留 treasury/interest 调整与 trade-leg/event 一致性；未把 100 用户现金总量用于 200 用户。visibility 通过持久 trade→pool-event ID 映射，度量范围明确为首个公共 observer。
- `summarize.py:27–86` 保留 12 格维度、missing/incomplete、完整性结果、abort reason、响应截止 TPS 与周期 TPS 的区别，以及 POST/cycle 延迟、资源/SQL、SSE、连接数、跳过/拒绝和 visibility。未发现汇总把 baseline/candidate 路径或用户数混淆的情况。

未发现会阻止矩阵执行或使本次 A/B 失真的新增具体错误。主 agent 报告 baseline 短 pilot 已完成且连接数/完整性正常；这属于主 agent 的执行证据，本审阅未重新运行。正式各格仍需核对实际连接数、abort/incomplete、调度迟滞和完整性；一秒资源采样、单轮 40 秒、首 observer visibility 及单 pair 场景的既定限制应保留在结果解释中。

## 问题清单

无需要提交修复的问题；无对应严重度、受影响行为或最小修复请求。慢公共组帧仍会串行拖延其他 pair、通知 best-effort、全部订阅者交付和生产代理行为未验证，这些是既有且已声明的边界，不新增为本轮需求。
