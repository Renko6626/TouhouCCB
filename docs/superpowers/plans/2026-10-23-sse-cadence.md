# SSE 限频实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. 如用户另选委派，再使用 superpowers:subagent-driven-development。步骤用复选框跟踪。

**Goal:** 将公共FX和私人pricing的发布工作限制到约4Hz，保留私人业务通知及公共成交增量。

**Architecture:** 公共FX沿用单worker与有界pair队列，空闲阻塞于queue.get，首项到来后开启250ms合并窗口再处理一批；私人publisher沿用wake与1s补偿，在读取前应用250ms最小轮次间隔。无需新组件、队列或每pair计时状态。

**Tech Stack:** 现有Python asyncio、FastAPI、SQLAlchemy、pytest；无新增依赖。

**Spec:** [限频设计](../specs/2026-10-23-sse-cadence-design.md)。背景：[调研](../../sse-asgi-review-2026-10-23.md)。

状态：原草案已完成独立Sol High审阅并落实修订；本版按用户确认调整为空闲阻塞、有更新再合批，由主agent复核增量，未另做独立审阅。用户随后明确授权Sol Medium实施、Sol High审阅及100/200人不同频率压力对照。实现和独立审阅已完成，100人性能测验完成；200人已按用户要求取消，追加4Hz对照完成。[审阅记录](../reviews/2026-10-23-sse-cadence-plan-review.md)。规划以`8904d1a`为基线；开工时重新检查HEAD和工作区差异，保留用户已有文档及无关修改，不覆盖未提交工作。

## Global Constraints

- 默认间隔0.25秒；公共只在拿到首项后开启合并窗口，空闲不新增周期唤醒。目标是发布工作最多约4Hz，不承诺250ms网络交付。
- 只改公共FX与私人pricing（含LMSR pricing）；LMSR公共tick保持现状。
- `account_changed`、`fx_order_result`提交后原样即时入FIFO，不合并、不等待pricing节拍。
- 首次snapshot/sync、15秒复核、1秒pricing补偿、认证/截止、断连、容量/满关闭、数据权威全部保持。
- 公共FOR SHARE、精确24h量、catch_up、完整事件增量、overflow失效保留；不重写SQL或缓存。
- 不改ASGI/风控、writer资金路径、订单扫描、schema、前端或部署；不增加可靠通知机制。
- 已有best-effort失败边界保持；不宣称通知必达，不顺手修图表异常恢复或跨标签页同步。
- 现有相关检查集中运行；新增测试限具体行为缺口，不做源码字符串/常量/内部调用次序断言，不扩展大型测试环境。
- 不自动推送、合并或部署；文档审批通过后才开始实现。项目AGENTS优先于技能的全量TDD/测试扩张要求。

## Review Focus

- 公共空闲/窗口内首项：空闲阻塞不轮询，drain不遗漏队列外持有的首项，取消时task_done恰好一次（Task 1）。
- 公共发布期间又到同pair更新：后续批次仍发布最终状态，drain不会提前结束（Task 1）。
- 一热点pair重复到达且有其他pair：每批各至多一次，不因某pair等待节拍增加另一pair延迟（Task 1）。
- 私人pricing等待期间多张订单/强平通知：原用户FIFO保留每条业务通知，不被节拍截留（Task 2）。
- 更新落在pricing等待与读取的交界：等待中取最新值、读取中的新wake留给下一轮（Task 2）。
- 一次合并涉及多个公共事件/缓冲溢出：保留全部保留中的增量或显式invalidation，不只留最新价（Task 3）。

## 文件与职责

| 文件 | 责任 |
| --- | --- |
| `backend/app/services/fx/publisher.py` | 公共事件触发合批，保留原enqueue、队列和生命周期 |
| `backend/app/services/trading_prices.py` | 私人publisher读取前最小间隔，不改registry数据来源 |
| `backend/tests/test_fx_publisher.py` | 公共合并、批次、公平性、生命周期；复用已有hooks |
| `backend/tests/test_trading_stream.py` | 私人完整输入、真实流与通知隔离；复用seed/request_stream |
| `backend/tests/test_fx_stream.py` | 公共wire增量与invalidation；仅必要时扩展现有用例 |
| 本计划及设计、独立审阅记录 | 记录批准、完成证据和实际限制 |

不新增生产模块或抽象通用节拍框架。常量分别放在两个现有模块，命名 `PUBLISH_INTERVAL = 0.25`；相同数值无需另建共享配置模块。下述所有pytest命令均从backend目录执行，`python`统一替换为已检查依赖的项目解释器，并保留 `env -u DATABASE_URL`，不使用外部导出的数据库URL。

## Task 1：公共FX空闲阻塞与事件触发合批

**Files:** Modify `backend/app/services/fx/publisher.py`、`backend/tests/test_fx_publisher.py`。

**Interfaces:** 保留 `FxPublisher.enqueue(pair_id, post_price, *, trade_id=None) -> bool`、`start/stop/drain`、`stats`及模块级hooks；`_publish(publication)`仍消费最新FxPublication并调用原market_data链。外部调用者无需修改。

- [x] 在现有生命周期/合并用例中补足空闲边界：worker确实等待队列数据而未进入合并等待；enqueue后才进入局部节拍屏障，此时首项虽已不在queue，drain仍不能完成；释放窗口后可正常发布、排空并重新进入队列等待。检查实际发布和drain结果，不仅统计sleep调用。覆盖停机/取消发生在空闲等待或窗口内持有首项的清理，不另搭测试框架。
- [x] 扩展现有publisher测试，先明确以下可观察要求并运行定位用例确认旧实现缺少限频：同批多次enqueue最终价被保留；持续更新的同pair相邻两次实际hook开始间隔≥250ms；发布期间新增更新最终也能drain完成。保留原coalescing和容量断言，不为证明限频放宽它们。
- [x] 增加或收敛一个A/B行为用例：首项A启动合并窗口，在窗口内enqueue B并更新A，窗口结束后A、B都属本批，A的hook内再触发新A；这一批B仍发布，A更新到下一批，结果为A、B、A且下一批间隔满足节拍。用模块局部的节拍Event屏障控制批次：释放首轮，A hook内enqueue新A；进入下一次等待且尚未放行时，实际hook输出必须已为[A,B]，放行后才得到最后A。只替换publisher模块的asyncio引用，不能修改共享asyncio.sleep。不写另一份调度算法作expected，不仅检查sleep调用次数，不用窄墙钟上界。
- [x] 在现有单worker实现事件触发批次：先await queue.get取得首项，保持其pending/queued，再sleep常量；窗口结束以1+qsize为本批预算，先首项再其余FIFO。预算内沿用原pop/publish/finally/requeue。首项在等待前就进入try/finally保护，窗口取消也恰好task_done一次，正常处理不重复确认。同批新入队的pair及in-flight重排项不超预算处理。保留原queue.task_done、drop计数与idle判定。
- [x] 沿用并验证无观看者跳过组帧、overflow合并、发布异常不中止worker、stop拒收且原超时drain、重复start/stop。特别检查idle必须等持有首项及后续重排清空，不能只看queue暂空或本批完成；worker不能改成 `while self._running`，否则stop拒收后无法继续drain。
- [x] 运行 `env -u DATABASE_URL python -m pytest -q tests/test_fx_publisher.py`，通过后保留输出。只修此改动导致的问题；不新增每pair worker/deadline表、额外锁或恢复队列。

**完成条件：** 空闲阻塞不新增4Hz唤醒；首项到来才开启窗口，窗口内新值被合并。单worker有界合并仍成立；每pair每批最多一次，没有逐pair节拍sleep，完整最终状态可排空。慢IO仍可能顺序阻塞，文档明确这一限制。

## Task 2：私人pricing读取前限频

**Files:** Modify `backend/app/services/trading_prices.py`、`backend/tests/test_trading_stream.py`。

**Interfaces:** `PricingPublisher.subscribe/unsubscribe/start/stop`、`wake: asyncio.Event`、`notify_pricing_changed(keys)`、registry接口不变。新增 `run`局部 `last_round_started: float | None`，每次run启动初始化为None。

- [x] 复用seed/request_stream及真实订阅生产逻辑，补一个连续wake行为用例：一个间隔内多次更新最终发布最新完整输入，后续轮次开始间隔≥250ms；等待期间update不丢，读取期间的新wake最终能触发后续发布。可在读取边界放一个最小Event屏障，输出仍断言真实registry/stream结果，不仅断言stub被调用。
- [x] 在 `run` 中保留1s wait_for补偿；唤醒/超时后按monotonic距上轮读取开始计算剩余间隔，必要时sleep。**等待结束后、读取前clear wake**，然后记录本轮开始时间。保持原读取、hash判变化、missing_groups、编码复用和异常日志。首轮立即，超时不做catch-up突发。
- [x] 扩展现有私人流用例：先有一轮pricing建立节拍，再在下一轮待发布时发本用户两个不同order_id终态、一个liquidation账户提示和另一用户提示。在publisher的节拍等待设置局部Event屏障；确认进入等待后再enqueue通知，在屏障未释放时，通过原request_stream收齐本用户通知，核对ID/终态/reason及他人隔离，最后释放屏障检查pricing。只替换trading_prices模块的asyncio引用，不修改共享asyncio.sleep，不冻结路由/数据库，不新增生产测试钩子。此处不用真实250ms窗口作成功上限，也不新增broker或队列。
- [x] 复用已有sync恢复、过期/满队列清理、持续流下rules/ban复核及订单结果用例。不改变原先有序入队关系；不把“立即入队”断言成网络恒定毫秒内必达。
- [x] 运行 `env -u DATABASE_URL python -m pytest -q tests/test_trading_stream.py tests/test_account_events.py`。不调整user_trading路由、enqueue、账户版本或复核周期来迎合测试。

**完成条件：** 重复wake在昂贵读取前合并，正常owner继续读原内存投影；私人业务通知不经pricing等待，控制帧和恢复行为保持。

## Task 3：增量边界与集中验收

**Files:** Review `backend/tests/test_fx_stream.py` 现有增量/invalidation用例；必要时仅在该文件补合并链覆盖，更新本计划验证记录。生产market_data/runtime/writer不改。

- [x] 先确认现有 `test_publish_pair_frame_drains_deltas_once_after_owner_catch_up` 与 `test_publish_pair_frame_surfaces_buffer_overflow_as_invalidation` 的覆盖。优先扩展前者，让真实FxPublisher在一批内合并多个publication，调用原publish_pair_frame与真实broker，验证传入的多个事件ID都进入帧。现有该用例使用_StubRuntime/FakeDB，沿用它们；证据定位为调度到原组帧接口的多事件字段保留，不声称验证真实runtime的512溢出或真实跨零资金执行，不为此新建集成环境。
- [x] 上一步若只能用stub验证转发，结果只写“保留原增量组帧接口/字段”；不能宣称完成真实跨零交易端到端验证。实际跨零全部执行腿的已有用例保留，除非发现本次调度涉及的独立缺口，不再复制完整金融场景。
- [x] 集中运行下列已有相关检查（并包含Task 1/2新增用例），记录命令、退出码和失败原因；这是本轮最终验证，成功后不无理由反复全跑：

  ```bash
  cd backend
  env -u DATABASE_URL python -m pytest -q \
    tests/test_fx_publisher.py tests/test_trading_stream.py \
    tests/test_account_events.py tests/test_fx_stream.py \
    tests/test_tick_broadcaster.py tests/test_market_read_state.py
  ```

- [x] `python`须指向有项目依赖且支持asyncio.timeout的解释器。当前可用 `/tmp/sunyunbo/thccb-pr23-review-20261023/review-venv/bin/python`；不假设临时目录永远存在。conftest在未传DATABASE_URL时建PID独立SQLite，不载入生产.env数据库URL，不访问生产。这里只验证调度与既有边界，不把SQLite通过写成PG锁语义新证据。
- [x] 用 `git diff --check` 及差异审阅确认仅两生产模块和相关测试/文档；检查loadtest原型对照仍明确是实验入口，本次不将其作为部署路径，也不静默改写历史报告。
- [x] 文档记录实际通过/未验证项：没有当前生产性能、真实代理/公网、全部订阅者逐条可靠交付或崩溃注入证据；不引用旧+54.8%作为本次收益承诺。

**完成条件：** 新节拍行为和已有消息语义检查通过；本轮没有扩大为数据库优化、图表恢复、ASGI或部署任务。

## 审阅与交接

文档提交独立审阅时重点检查：事件触发批次是否遗漏窗口内首项或in-flight最终更新、首项取消是否正确task_done、队列容量/idle是否改变、私人clear wake位置是否丢提示、验证是否真正覆盖通知旁路且保持范围。具体问题在本文修正并记入独立审阅记录；理论边角风险不自动新增机制。

用户已确认本次事件触发合批的计划调整；收到后续实施指令后，默认由主agent在当前会话按任务实施；如用户另选委派再调整执行方式。完成代码后做一次固定diff审阅；不自动发布。若出现需要按pair截止时间、新缓存或其他边界的决定，先回到用户确认。

## 实际执行记录

Sol Medium在隔离分支perf/sse-cadence-20261023完成实现，commit e9c0242019cc5aecd4cbcd29ca5d27b4191cee62。实施前六文件50 passed，最终相关检查55 passed in10.52s；Sol High规格符合性及代码质量均approved。主agent将审核过的5文件差异应用回主工作区，并逐字核对与已测worktree一致；没有推送或部署。详见[代码审阅](../reviews/2026-10-23-sse-cadence-code-review.md)与[验证记录](../../../output/sse-cadence-ab-20261023/validation.md)。

新增授权的性能矩阵：100/200用户、各2条SSE、全站合计1/20/54次流程每秒，40秒测量与10秒预热；baseline8904d1a对照e9c0242，仅两生产模块差异，不启用旧ASGI或读进程原型。正式结果见[性能报告](../../../output/sse-cadence-ab-20261023/report.md)（结果完成后写入）。

## 低负载修订记录

用户指出无条件125ms轮询在低负载下可能增加无用唤醒，已同意改为空闲await队列、有更新再开启完整125ms窗口。本版将批预算从qsize改为1+qsize，补上队列外首项的pending/queued、drain与取消确认边界；私人pricing仍沿用原事件驱动加1秒补偿，无固定8Hz空转。原独立审阅针对固定tick版本，本次变更仅做主agent静态复核；未执行测试或产品实现。

## 4Hz试验及测试范围调整

用户取消200人组：100人baseline/8Hz的1、20、54次流程每秒已完成；200人部分原始记录保留但不纳入结论。当前默认改250ms（4Hz），Sol Medium最小提交7406d9c仅两生产常量及两测试阈值，相关检查55 passed in10.37s，独立Sol High增量审阅另记录。随后只测100人20/54两档，各40秒和既有10秒预热，复用已审压测工具，不重跑整套矩阵。私人通知和LMSR公共8Hz均保持。

## 本轮完成记录

当前实现7406d9c（4Hz），独立Sol High增量审阅approved，55项相关检查通过；主工作区与已测实现逐字一致。100人baseline/8Hz各1/20/54、4Hz仅20/54，共八格有效运行。200人不纳入结论，临时PG与压测进程已停止，无推送或部署。详见[最终性能报告](../../../output/sse-cadence-4hz-20261023/report.md)与[4Hz审阅](../reviews/2026-10-23-sse-cadence-4hz-code-review.md)。

## 正式落地确认

用户在比较后选定4Hz，并要求审阅通过后实装。主agent复核当前五个代码/测试文件与独立Sol High已审7406d9c逐字一致；公共FX和私人pricing均250ms，LMSR公共仍125ms，账户与订单结果路径未改。沿用55项通过及已完成100人性能证据，不重复启动测试或压测。代码及必要文档提交本地仓库；未推送、未触发Coolify生产发布。原始请求/资源采样继续保留在本地output目录，提交包含汇总与完整性数据及复测工具。
