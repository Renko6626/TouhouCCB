# SSE 限频与直接 ASGI 中间件评估

审阅日期：2026-10-23。代码基线：`8904d1a362cfb28b19ceba53a2538e3c2cf7edff`，即 PR #23 合并后的 main。两名 Sol High 子 agent 分别审阅 SSE 与 ASGI，主 agent 汇总并复核关键证据。本次只新增研究文档，没有启用原型或修改生产实现。

## 结论与推荐顺序

**优先研究行情限频，保留私人账户、订单结果的原有即时发送路径；ASGI 可以作为独立的局部改造，暂不与读进程分流捆绑实施。**

| 方案 | 判断 | 正式采用前必须明确的边界 |
| --- | --- | --- |
| 公共 FX 行情与私人 pricing 约 8 Hz | 有价值，优先级较高；减少读库、组帧和长连接扇出 | 合并更新提示和价格快照，完整保留成交增量及失效标志；公共多币种不能共用一个会 sleep 的发布通道 |
| 私人账户与订单结果限频 | 本次不建议 | `account_changed`、`fx_order_result` 保持提交后即时入队，不覆盖、不为凑行情帧等待 |
| 直接 ASGI 中间件 | 官方常规写法，维护成本可控；有压力时值得单独采用 | 正式替换现有日志/访问策略层，保留策略、顺序与 FastAPI 处理；不用 loadtest 入口部署 |
| 独立只读进程、跨进程消息、可靠通知回放 | 不纳入本次建议 | 属于另一个需求或架构决策，不能用历史组合方案的收益自动授权 |

玩家关心的关键区别是：**资金执行和订单终态持久化不依赖浏览器收到 SSE；私人通知本来就没有逐条必达保证。** 原型不合并这些私人结果，但断连后恢复最新余额、持仓，与补回每一条订单弹窗是两种能力。前者已有恢复路径，后者尚未提供。

ASGI 收益还存在一个重要版本差异：历史压测只有一层 `BaseHTTPMiddleware`，当前新增风控层后有两层。替换日志层仍留有一层流式中继，不能把历史约 20.6% 增益直接用于当前版本。

## 一、SSE 限频

### 当前各通道到底多快

| 通道或事件 | 当前生产行为 | 本次可改变的部分 |
| --- | --- | --- |
| LMSR 公共 tick | dirty 市场按 0.125s tick 发布，实际间隔包含处理时间 | 已经约 8 Hz，无需再限一次；帧内 trades 列表不能被最新价格覆盖 |
| FX 公共行情 | 提交后 enqueue，单 worker 发布；同 pair 已会合并待处理提示，没有固定频率上限 | 在读库和组帧前限制发布频率，保留 committed pool events 增量 |
| 私人 `pricing` | wake 事件驱动，另有 1s 超时补偿；输入 hash 变化才发 | 可合并中间价格输入，按约 8 Hz 读取、组帧，保留全部订阅依赖组 |
| 私人 `account_changed` | 资金提交后立即入队 | 保持原样；前端原有约 200ms 刷新请求合并不等于服务端丢事件 |
| 私人 `fx_order_result` | 挂单 scheduler 取得已提交终态后单独入队 | 保持每张单的终态结果，不能只留一窗口最后一张单 |
| 私人控制信息 | 首帧 sync、每 15s 检查用户/规则/依赖并发 heartbeat；连接受 JWT/最长 1h 限制 | 不纳入行情限频 |

特别注意：私人 pricing 的 **1 秒是补偿超时，不是当前 1 Hz 上限**。

代码依据：[tick_broadcaster.py](../backend/app/services/tick_broadcaster.py) 第 44、53–94、116–120 行；[fx/publisher.py](../backend/app/services/fx/publisher.py) 第 164–219 行；[trading_prices.py](../backend/app/services/trading_prices.py) 第 264–305 行；[account_events.py](../backend/app/services/account_events.py) 第 59–68、92–97、121–129 行；[user_trading.py](../backend/app/api/v1/user_trading.py) 第 70–135 行。

### 原型的实际作用与多币种问题

[fx_capacity_server.py](../loadtest/fx_capacity_server.py) 第 69–96 行只做两件事：

1. 将 pricing 的 wake 换成 `CadencedEvent(1/8)`，唤醒后等待距上次发布机会至少 125ms，再读最新状态。等待期间的重复唤醒合并为一次读取。`asyncio.Event` 本来就是布尔标志，重复 set 不表示积攒了多项任务。[Python Event 文档](https://docs.python.org/3.13/library/asyncio-sync.html#asyncio.Event)
2. 在公共 FX `_publish` 前按 pair 插入 sleep，仍沿用原来的提示合并与已提交事件补读。

它没有包住私人账户或订单结果的 enqueue，也没有改资金提交、挂单扫描、HTTP 回执和逐腿账本。

不过公共 publisher 只有一个 worker，A 币种等待自己的 125ms 节拍时，B 币种也会被挡在后面。用真实 `FxPublisher` 和原型 wrapper 的临时无数据库诊断，发送顺序为 `[A,A,B]` 时，新 B 等待约 **125.2ms**，期间 drop=0。这里已经复现了队首等待，不能把单热点币种原型原样用于多币种生产。

正式方向应让未到发布时间的 pair 不占住整个发布 worker；采用何种小型按 pair 调度方式，应在实施前确认。无需由此引入一套通用任务调度平台。8 Hz 也只表示约 125ms 的发布机会间隔，读库、排队、网络和消费者背压仍会增加端到端延迟。

### 玩家挂单和平仓事件会不会被漏

| 操作 | 实际提交与通知链 | 对限频的判断 |
| --- | --- | --- |
| 创建、撤销挂单 | 更新订单表，不预占资金、不推进经济版本；HTTP 返回后当前页面重拉列表 | 本来没有专门的创建/撤单 SSE，不能将其误判为被限频吞掉 |
| 挂单成功 | 同一资金事务中写交易、执行引用及 succeeded；提交后发账户提示，scheduler 再构建该订单结果并入私人队列 | 两类私人提示都不应限频；一窗口触发多单仍应逐条入 FIFO |
| 挂单失败 | 失败终态持久化后，由 scheduler 构建该订单结果 | 保持独立结果，不与价格帧合并 |
| 手动平多、平空、普通成交 | writer 提交并安装最新定价，再发私人账户提示、公共提示，最后返回 HTTP | 私人账户提示不经公共 publisher 或 pricing wake，限频不删除资金执行 |
| 强平 | 提交后发 `account_changed(reason='liquidation')`，再通知公共行情 | 保留原即时路径，不能仅因为最终账户相同而覆盖掉 reason |
| 跨零交易 | 一次执行可能包含多条 legs，每条追加持久 pool event；HTTP 回执保留全部 trade_ids | 公共提示可合并，但必须补读全部 committed events；不能仅按最后 trade_id 组帧 |

挂单创建/撤销依据：[fx/orders.py](../backend/app/services/fx/orders.py) 第 15–34、250–264 行；[fx/writer_ops.py](../backend/app/services/fx/writer_ops.py) 第 289–327 行；[fx.py](../backend/app/api/v1/fx.py) 第 282–299 行。当前页面在 [FxTriggerOrders.vue](../thccb-frontend/src/components/market/FxTriggerOrders.vue) 第 71–100 行操作成功后 reload；打开管理面板及手动刷新也会读最新记录。其他标签页即时同步尚无对应保证。

挂单终态依据：[fx/orders.py](../backend/app/services/fx/orders.py) 第 61–78、139–204 行；[fx/order_scheduler.py](../backend/app/services/fx/order_scheduler.py) 第 45–60 行。scheduler 默认 10s 扫描、最小 5s，与行情限频是不同设置。

普通执行、强平依据：[fx/writer_ops.py](../backend/app/services/fx/writer_ops.py) 第 123–173、359–392 行；LMSR 对应 [market_writer.py](../backend/app/services/market_writer.py) 第 323–339 行。跨零依据：[fx/net_trading.py](../backend/app/services/fx/net_trading.py) 第 314–365 行及 [fx/pool_events.py](../backend/app/services/fx/pool_events.py) 第 8–23 行。

账户 SSE 提供的是“账户已改变、应读取最新状态”的提示，不提供每个中间仓位，也不保证每次经济版本自增对应一帧。执行腿和成交凭据由 HTTP 回执与数据库保留。这一语义允许合并价格投影，不能据此覆盖订单终态通知。

### 现有可靠性边界与恢复能力

私人三类帧共用每连接 **64 帧 FIFO**。队列满时将连接标为 closed，流下一次推进时结束，不保证把已排队的全部帧发完。减小 pricing 流量有望降低队列压力，但不能将其等同于私人事件必达。[account_events.py](../backend/app/services/account_events.py) 第 18–35 行。

订单终态已提交后，若进程在构建/入队结果前停止，或短会话读取、通知构建失败，可能漏掉即时提示。scheduler 有 `Committed private FX order result dropped` 日志，后续扫描 waiting，不会自动重发已终态订单；无人订阅时也不保存通知等待上线。数据库仍有订单终态。这是已有 best-effort 边界，并非此次限频新增行为。

私人连接用 fetch-SSE 重连，不发送 Last-Event-ID，服务端也没有事件 `id:`。重连首帧 sync 触发账户刷新；可见页面每 15s 校对版本，回前台也会检查，因此漏一条账户提示通常可通过权威读取修复最新余额、债务和仓位。**具体订单弹窗、强平提示不会因此逐条重放。** 自动重连本身也不会生成服务器未实现的历史补发能力。[WHATWG SSE 标准](https://html.spec.whatwg.org/multipage/server-sent-events.html#the-last-event-id-header)

前端恢复依据：[tradingStream.ts](../thccb-frontend/src/api/tradingStream.ts) 第 43–79 行；[tradingSession.ts](../thccb-frontend/src/stores/tradingSession.ts) 第 148–216、243–244 行。挂单组件观察 lastOrderResult 后重拉，但没有监听 sync/账户版本来保证重连后重拉，因此漏结果时，已打开的列表可能暂时陈旧；重新打开或点“刷新记录”可修复。

按当前使用规模，可以先接受查订单/成交记录及手动刷新。若需要改善体验，优先单独评估重连或打开面板时重拉列表。只有明确要求离线逐条补通知时，才讨论持久通知、ACK、outbox 或回放机制；它们不是行情限频的默认前置条件。

公共链也不能整帧取最后一个值：

- publication queue 默认容纳 256 个待发布 pair ID；同 pair 合并，满或停止会 drop 并计数，发布异常有日志。提示丢失后的现有连接不保证立即更新，下一笔发布或重连/刷新可修。
- 每 pair public delta buffer 为 512 个 event，溢出会清空并置 invalidated；客户端应依据失效标志重读图表。不能只留下最新价格而丢掉这个标志。
- 公共连接队列 32 帧，慢消费者满队列会被 detach/kicked，重连重新获取 snapshot。FX 恢复依赖 snapshot、history_invalidated/history_version 及图表重载；不能套用 LMSR 的 seq gap 检查，因为 FX parser 没有把外层 seq 传给组件。

相关代码：[realtime.py](../backend/app/services/realtime.py) 第 67–85、110–145 行；[fx/market_state.py](../backend/app/services/fx/market_state.py) 第 366–380、801–809 行；[api/fx.ts](../thccb-frontend/src/api/fx.ts) 第 449–519 行；[FxCandleChart.vue](../thccb-frontend/src/components/chart/FxCandleChart.vue) 第 696–738 行。

另记录一个静态可达的已有图表风险：[fx/market_data.py](../backend/app/services/fx/market_data.py) 第 235 行先 drain delta，再继续读库、组帧、发布；之后异常时有 failed 计数/日志，但未见把已 drain 数据放回或自动置 invalidated。某批实时图表增量因此可能暂时漏显，直到刷新、重连或其他重载触发修复。持久历史与资金不受影响。本次未注入故障复现，不将其写成已发生事故，也不把修复它作为限频必须顺带完成的需求。

## 二、直接 ASGI 中间件

### 它保留什么，替换什么

纯 ASGI 中间件是 Starlette 官方支持的写法。它继续由 FastAPI 处理路由、鉴权、依赖注入、请求校验和异常，不会自动取消业务层事务或所有权检查。[Starlette 0.50.0 中间件文档](https://github.com/Kludex/starlette/blob/0.50.0/docs/middleware.md#pure-asgi-middleware)

当前入站链如下，注册顺序及构造依据见 [main.py](../backend/app/main.py) 第 392–463 行与 [Starlette applications.py](https://github.com/Kludex/starlette/blob/0.50.0/starlette/applications.py#L77-L99)：

```text
ServerErrorMiddleware
  → BaseHTTPMiddleware(record_risk_request)
    → BaseHTTPMiddleware(log_requests)
      → CORSMiddleware
        → ExceptionMiddleware → FastAPI 路由、依赖、端点
```

`log_requests` 虽以日志命名，实际还承担非安全方法的只读/所有权拦截、API no-store/Pragma 头、跳过前缀、状态及异常日志。正式替换必须保留这些职责。

原型只在 `app.user_middleware` 的原位置，将 dispatch 为 `log_requests` 的那一项替换为 `DirectASGIMiddleware`，断言恰好一项。当前替换后链为：

```text
ServerError → risk BaseHTTP → DirectASGI(原访问策略) → CORS → Exception/FastAPI
```

它在 `http.response.start` 设置头和记录状态，后续消息直接 `await send`。非 HTTP scope 透传，不改 lifespan 或 WebSocket。这里减少的是一层请求/响应适配；现有风控层仍会转发每个 SSE 数据块。

`BaseHTTPMiddleware` 的 `call_next` 通过下游任务和 AnyIO memory stream 接收响应，再用流式响应向外转发。日志分支对 SSE 提前返回，仍要经过这些任务与通道。纯 ASGI 在被替换这一层省掉了该中继，没有提高数据库并发或让同线程 Python CPU 并行。[Starlette 0.50.0 base.py](https://github.com/Kludex/starlette/blob/0.50.0/starlette/middleware/base.py#L98-L182)

### 风险与维护成本

| 项目 | 审阅结果 | 正式移植注意点 |
| --- | --- | --- |
| 写权限及所有权 | 原型复制同一方法集合、环境/reason 条件及 require_writes/503；未绕过业务层门禁 | 属关键行为，逐段核对并运行已有只读门检查 |
| CORS、缓存与重复响应头 | 原位置替换保留 CORS 顺序，MutableHeaders 仅改两种缓存头 | 保留注册顺序，不把多条 Set-Cookie 转成普通 dict；历史段缓存继续原样 |
| 只读503及未处理500 | 两版均存在没有 CORS/no-store 的短路或最外层错误响应 | 是既有行为，本次不顺手重排整个异常/CORS链 |
| 响应前异常 | 记 EXC 后继续向外抛，由现有异常链处理 | 不吞异常，不自己补另一套500响应 |
| 响应后/后台任务异常 | 两版均可能先记200，再向服务器抛后续异常 | 状态日志不代表整段流成功；无需为此新增完成状态机 |
| SSE断连与取消 | receive 透传，没有吞 CancelledError，生成器清理由原流式响应处理 | 拓扑变化后仍需核对真实公共/私人流的订阅清理 |
| 慢客户端与背压 | 继续 await send，没有新增无界缓冲 | 省掉的是中继，不保证客户端收到；外层risk仍有中继 |
| 后台任务 | 仍 await 整个下游应用，不提前返回 | 核对一次后台回调，无须触发真实管理重启接口 |
| contextvars、并发隔离 | 新类请求状态在单次调用内；本层不再建下游任务，context传播会改变 | 当前未见业务显式ContextVar；risk层仍有原限制，不宣称全栈问题消失 |
| 日志计时 | 改为下游发出response.start时记录，少一段通道调度时间 | 两版都近似“到响应头”，均不是完整流持续时间 |
| 实验注入方式 | 函数身份匹配、修改user_middleware、混合采样/lifespan及行情patch | 这是主要生产化维护问题：迁入正式模块，公开注册，不用loadtest入口 |

错误边界依据：[Starlette errors.py](https://github.com/Kludex/starlette/blob/0.50.0/starlette/middleware/errors.py#L149-L186)；后台任务依据：[responses.py](https://github.com/Kludex/starlette/blob/0.50.0/starlette/responses.py#L148-L160)；contextvars 限制见[官方说明](https://github.com/Kludex/starlette/blob/0.50.0/docs/middleware.md#limitations)。ASGI 的 `send` 表示交给协议服务器处理，不能视为客户端消费确认。[ASGI HTTP 规范](https://asgi.readthedocs.io/en/stable/specs/www.html#response-start-send-event)

建议的最小正式方案是将原策略整理为约 60–90 行带类型的 ASGI 类，在现日志装饰器的位置正式注册，继续位于 CORS 之后、risk 注册之前。共享前缀/头部规则就近放置，避免从中间件反向导入 app.main；不设计通用框架，不一起重写风控层。

工程估计为半天至一天，主要工作是正规注册、保持策略及有限验证；这不是实际测得的工时。后续维护集中在 Starlette/Uvicorn 升级时的顺序、异常和流式响应行为，不增加运行组件。少量用户低频访问时，保留现状也合理。是否正式采用需要独立确认。

## 三、如何解读性能数字

下表来自 [comparison.json](../output/fx-capacity-54-20261018/comparison.json)，是历史实验，不是当前 main 的新压测。

| 历史方案 | 成功交易/s | 交易 POST P99 | 能说明什么 |
| --- | ---: | ---: | --- |
| 已做 SQL 优化的 current | 24.13 | 3940ms | 当时完整 HTTP/SSE 合成负载的基线 |
| + cadence8 | 37.37 | 2166ms | 减少行情读取/扇出的潜力；本轮约 +54.8% |
| + ASGI | 45.07 | 1854ms | 在 cadence8 基础上，当时约 +20.6%，并非独立方案的普遍增幅 |
| + 两类 GET 独立只读进程 | 53.60 | 79ms | 同时改变进程分工，额外使用 reader CPU，不能全归因于前两项 |

**当前中间件链已变化。** `git show 75d33eb:backend/app/main.py` 只有一个 HTTP middleware，无 record_risk_request；当前两个。`git blame` 显示风控层由 2026-10-20 的 `0e0d60dc` 加入。三个压测脚本 SHA256 仍与 environment.json 匹配，但脚本导入的 app.main 随 checkout 变化；脚本相同不等于被测应用相同。因此当前仅改日志层的边际收益可能更低，需要当前版本对照才能定量。

其余口径限制也影响决策：

- 固定合计 54 次流程/s、100用户、200长连接、单热点币种；额外 HTTP 报价及多个交易后 GET，比普通前端本地报价流程更重。
- i7-14700/WSL2、本机 PostgreSQL 与 Unix socket，没有生产 TLS/代理/公网。每个正式方案只有一轮60s，只有最终三项组合做300s。
- cadence8 与 ASGI 两组仍未完成54/s目标，分别跳过873、372次计划流程；不能仅看已提交交易成功率。
- visibility 仅记录第一个公共订阅者的 pool_event_seen，并按数据库 event ID 对应执行腿。missing={}不证明全部订阅者逐条收到，更没有逐用户审计私人挂单/强平结果。
- P99 大幅下降包含从过载排队转为接近可承受负载的效果，不能解释为数据库执行本身快几十倍。

原始方法与环境见[容量报告](../output/fx-capacity-54-20261018/report.md)、[environment.json](../output/fx-capacity-54-20261018/environment.json)；可见性采样见 [fx_http.py](../loadtest/fx_http.py) 第 254–258 行和 [fx_capacity.py](../loadtest/fx_capacity.py) 第 335–345 行。

## 四、本次验证与实施前最小检查

### 已实际完成

1. SSE agent 在 backend 运行：

   ```bash
   env -u DATABASE_URL /tmp/sunyunbo/thccb-pr23-review-20261023/review-venv/bin/python -m pytest -q -x tests/test_account_events.py tests/test_trading_stream.py tests/test_fx_publisher.py
   ```

   当前实现 **22 passed in 3.03s**。覆盖独立用户投递、队列溢出、回滚不发、通知失败不回滚资金、sync、订单结果和公共发布行为，使用一次性 SQLite，未连接生产。

2. 临时加载 asgi_cadence8 wrapper，移除性能采样 lifespan wrapper 后，运行相同三个已有测试文件：**22 passed in 3.42s**，有两条插件已导入的 pytest 警告。它说明这组现有检查在原型下也通过，不证明真实网络交付或所有新时序。
3. 无数据库多 pair 诊断：真实 publisher 与原型 cadenced_publish，复现 B 被 A 的节拍等待阻塞约 **125.2ms**。
4. ASGI agent 用真实策略函数/原型类按当前顺序搭建纯内存 Starlette 链，9类新旧对照一致：普通 GET/POST、只读503、CORS预检、响应前异常、响应后异常、后台异常、断连和任务取消。主 agent 重跑保存的临时脚本，退出0，输出 `PASS: 9 targeted old/direct chain comparisons`。

   ```bash
   /tmp/sunyunbo/thccb-pr23-review-20261023/review-venv/bin/python /tmp/thccb-asgi-diagnostic-20261023.py
   ```

   该诊断替换了所有权和记录器对象，没有启动真实 app 生命周期、鉴权数据库或网络；不能当作业务验收。
5. 复核历史/当前中间件数目、风控层 blame、脚本哈希与历史数值。仓库只新增本文，未新增永久测试、未运行新压测、未改部署。

### 确认实施后再做

限频只需围绕真实缺口集中验证：同一窗口多张订单与强平提示仍逐条入私人队列、多 pair 不互相 sleep 阻塞、跨零所有公共事件保留、buffer 溢出有失效信号、慢连接重连后账户与挂单列表的恢复边界。复用已有测试，少量缺口在最合适一层补足，不逐层复制场景。

ASGI 复用只读门、私人流隔离/清理、历史缓存检查，加有限头部/异常/后台回调对照；不重排risk/CORS。如果要定量承诺收益，再按当前 main、保留risk层、贴近实际页面请求比例做短程 A/B，避免把旧结果充当当前验收。

尚未验证生产机器性能、真实代理与公网、所有订阅者逐条投递、提交后进程崩溃等故障注入。现阶段不需要为研究引入大范围 E2E、30分钟压测或可靠消息架构。

下一步待确认的是限频目标及公共按 pair 调度方案；ASGI 是否另行实施可以独立决定。用户确认前，以上均为研究结论，不触发生产改动。

## 五、限频前的数据流与内存和数据库边界

本节基于当前代码补充，区分“当前查库”与“任何设计下都必须查库”。当前 owner 的私人 pricing 已有已提交内存投影，限频主要减少 CPU 和扇出；公共 FX 组帧仍有明确数据库读取，限频也会减少这一部分读频率。这里只梳理算法，没有改变缓存或一致性约定。

### 私人 pricing 已经如何复用内存

启动时 [main.py](../backend/app/main.py) 第 180 行为 FX/LMSR 启用内存模式，FX 预热在生产者启动前完成。FX writer 的顺序为：事务 flush → 捕获不可变快照 → 数据库 commit → install_committed → 私人账户提示及公共更新提示。失败事务不会发布未提交储备。快照是 frozen dataclass，保留 Decimal 储备和费率等普通值，不把 ORM 或会话长期放进 registry。

[trading_prices.py](../backend/app/services/trading_prices.py) 的 registry 在内存保留：

- 每个市场/币种的最新 committed state。FX 包括两侧储备、买卖费率、pool_version、状态、归档、reduce_only 等；LMSR 包括 b、outcome IDs、q、状态与截止时间。
- 每个 group 的 input_hash、revision，以及进程 epoch。hash/revision 在 install 时更新，publisher 用 hash 判断是否需要发变化帧。
- 按 group 的冷读锁，以及当前订阅连接所需的 group 集合。

可复用条件包括产品启用、持有写所有权、允许写入、非只读实例、writer 健康且记录存在。正常 owner 热路径在 `capture_committed_records` 同步取得引用，**读取定价输入不查数据库**。只读/未覆盖进程走数据库；记录缺失时有冷读路径，但 owner 的活跃市场缺少健康投影会拒绝返回503，而不是偷偷拿旧快照继续。这些约束不能因限频而删除。

每次发布轮次的算法为：

```text
收集所有连接订阅的 group，并集去重
  → 每个 group 共享读取一次 committed record
  → 转成响应 group，与上轮 input_hash 比较
  → 变化 group 各 model_dump 一次
  → 每种相同的 affected subset 编码一次，共享 bytes
  → 给需要它的各连接入队
```

它已经避免“100个用户各查一次同一币种价格”。但即使价格从内存取得，仍会扫描订阅集合、构造 group 对象、比较 hash、编码并逐连接投递；`notify_pricing_changed(keys)` 当前没有使用 keys 精确标脏，而是唤醒全局轮次。因此8Hz仍有收益。空变更能省发送，却没有省掉此前的扫描和对象构造。

**私人 pricing 不包含完整账户。** 用户余额、债务、仓位、挂单记录没有因此进入常驻权威缓存。连接建立时查用户身份、依赖和经济版本；每15s核对用户有效性/规则/依赖；收到 account_changed 时重查依赖，账户刷新另走 HTTP。依赖身份是一次 UNION ALL，包含持仓、FX钱包与空头的市场ID。不能把这些读取算成“每个pricing帧都查资金”。依据：[user_trading.py](../backend/app/api/v1/user_trading.py) 第 70–135 行；[credit/trading_context.py](../backend/app/services/credit/trading_context.py) 第 30–45、57–77 行。

### 公共 FX 是内存投影加数据库校验和补读

| 数据 | 内存保留什么 | 当前公共组帧何时查库及原因 |
| --- | --- | --- |
| 发布提示 | pair队列、最新publication、queued/pending集合 | 提示本身不查库；无公共订阅者时直接跳过整次组帧 |
| 池储备、费率与状态 | pricing registry 的不可变已提交 FxReadState | 每帧先 SELECT pool_version FOR SHARE；相同版本复用内存，版本不同时读新行 |
| 价格、买一单位/卖一单位价格、spread | 从已取得的储备和费率用 Decimal 本地计算 | 计算不查库，也不是每个观看者分别计算 |
| 图表近端数据 | 每pair的K线ring、已应用/已落库事件游标、历史版本、512条公共delta缓冲 | 发布前 catch_up 查已提交事件最大ID，只分页读取游标后的新事件；没有重新扫描整天成交明细 |
| 滚动24h成交量 | 此发布路径不直接使用内存ring合计作最终答案 | 单条SQL合并完整分钟K线、两侧不足一分钟的成交、尚未落库到K线的事件尾部；未ready则回退精确成交SUM |
| 历史就绪状态和版本 | runtime有本地副本及游标 | snapshot仍另读持久 FxMarketDataState，并与runtime历史版本比较；不一致则丢本帧增量并标失效 |
| SSE帧及订阅连接 | broker按topic保存连接和队列；同帧编码一次，共享bytes | 分发本身不查库；每个连接只入队引用，仍需各自网络发送 |

对应链路：[fx/publisher.py](../backend/app/services/fx/publisher.py) 第 224–247 行 → [fx/market_data.py](../backend/app/services/fx/market_data.py) 第 203–271 行 → [fx/trading.py](../backend/app/services/fx/trading.py) 第 207–222、794–846 行。runtime补读见 [fx/market_state.py](../backend/app/services/fx/market_state.py) 第 450–527、866–894 行；成交量见 [fx/market_reads.py](../backend/app/services/fx/market_reads.py) 第 693–798 行。

池共享锁不是单纯证明“内存有没有缓存”，它在读 quote、volume 和历史状态期间约束同pair写入，防止拿不同池版本拼出一个看似一致的snapshot。数据库可能已经commit但writer还没install内存；此时版本不一致就用锁住的新行。删除这个SQL/锁需要重新定义公开帧的一致性，不能当成直接去重。

精确24h量也不是简单把ring里1440个分钟相加：窗口两端通常落在分钟中间，历史K线可能尚未flush，旧成交会随时间越出窗口。现有单SQL读取持久游标、完整分钟和边缘/尾部，使这些部分处于同一语句快照，避免并发flush造成重计或漏计。它仍有计算成本，但已比每次装载全日ORM成交轻得多。

### 当前算法的判断与优化顺序

已有的内存边界总体合理：committed市场输入及可重建行情放内存，资金权威、成交证据、订单终态持久化；公开帧对版本与精确统计仍有数据库依赖。

优先把限频放在昂贵的组帧工作之前：公共FX在catch_up/版本校验/volume SQL之前合并提示，私人pricing在全局读取/对象转换/扇出之前合并唤醒。若先每笔查完、编码完，只在socket出口少发几帧，节约会小得多。后台runtime本身仍会按提交提示及5s补偿维护K线；只限制公共发布不会自动消除它的所有SQL。

本次无需额外引入缓存即可推进这一方向。后续有实测压力再评估以下局部候选：

1. **公共volume与元数据读取收敛。** 当前volume查询已读取ready/游标，而snapshot还单独查state；可以调查是否一次返回所需元数据，保留原快照和历史版本语义。这是候选，不是本次已验证的可直接删除SQL。
2. **pricing更早按stamp筛选。** 目前先构造groups再判变化，可评估健康committed record先比stamp、仅转换变化项。不能漏missing/recreate或新订阅初始sync。
3. **精确dirty group。** 当前notify忽略keys；订阅市场很多时才值得考虑按变化group处理。它涉及额外失效覆盖，不能仅凭理论复杂度就加机制。

不建议先把24h量、完整用户账户、依赖授权集合全部变成长驻缓存，也不建议直接从writer的可变执行对象读取。这些改动需要另行说明新鲜度、写路径覆盖、恢复和跨进程限制。当前最清楚的收益仍是降低重复工作发生的频率。

本节为代码路径审阅；没有测当前每帧SQL次数或CPU占比，没有新增测试或修改实现。
