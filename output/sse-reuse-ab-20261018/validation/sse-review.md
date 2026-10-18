# SSE 复用独立审阅

审阅范围：固定 `58101e1..d18445b`（HEAD 完整值 `d18445b8ca8e24f916a221b7b04e3798d223384d`）。已读 AGENTS、sse-brief、sse-report、完整 review diff，并沿实际生产调用与队列消费者核对。此次同时完成任务符合性与整体集成审阅，没有另设审阅席位。

## 结论

**批准此提交，可进入主 agent 后续统一性能对照。** 未发现需要修正的 Critical、Important 或 Minor 问题。优化限于私有帧的数据复用与构造；不能据此声称已证明实际 CPU 或吞吐收益。

## Critical

无。

## Important

无。

## Minor

无。

## 主要证据

- **编码及实际生产消费者匹配。** `account_events.py:12–15` 保留原 `json.dumps(..., separators=(',', ':'))` 默认行为，只将原先由 StreamingResponse 完成的 UTF-8 编码前移；项目 venv 的 `starlette/responses.py:253–256` 对 bytes 直接发送。`account_events.py:21–35` 的队列现在承载 `(event, bytes)`，唯一私有生产队列消费者 `user_trading.py:115、123–131` 解包并发送同一 wire。生产范围搜索未发现仍将其当 dict 读取的调用。
- **生产端确实共享同一不可变值。** `account_events.py:59–68、121–129` 对一次账户/订单发布各编码一次，并同步 offer 给现有订阅者；广播没有新增 await，不改变各连接 FIFO。`trading_prices.py:282–294` 仍每个 changed group dump 一次，局部 frames 只按本轮 `changed & wanted` 复用，没有跨轮缓存、降频、丢弃或合并消息。排序、groups/missing_groups 字段保持；同 affected subset 的 server_time 随共享帧一起复用，符合授权。
- **类型及异常边界没有发现实际缺口。** 定价 schema 的所有字段是 JSON 原始类型及其列表/嵌套模型（`schemas/trading_context.py:9–36`）；订单继续使用 `model_dump(mode='json')`，包括 datetime/Decimal（`account_events.py:127`、`schemas/fx.py:674–687`）；账户发布中的版本和 operation_ref 仍转字符串，实际 reason 生产调用均为字符串。账户和订单编码仍被原有非抛出发布 wrapper 捕获（`account_events.py:72–76、121–131`），不会向资金提交传播。定价循环仍有原有异常日志边界（`trading_prices.py:295–298`）。
- **未引入模块初始化循环。** `account_events.py:7` 原有依赖 trading_prices；反向导入仅在 `PricingPublisher.run` 执行时发生（`trading_prices.py:264–266`），`start` 只在运行时创建该任务（`250–252`）；两模块顶层没有调用 start。`user_trading.py:55–56` 的导入与这条延迟路径相容。
- **流语义保持。** `user_trading.py:73–93` 仍先订阅再重读依赖并输出 sync 首帧；`94–122` 保留绝对到期、断连与独立 15 秒鉴权/规则/依赖检查，以及 due 时保存 pending_frame；`127–130` 仍在输出 account_changed 前刷新依赖。`account_events.py:21、30–35` 仍为 queue64，满时关闭；完整 account_changed 和 fx_order_result 均入原队列。无新增恢复或排队机制。
- **公共流和业务边界未动。** 固定 diff 的生产变更仅 `account_events.py`、`trading_prices.py`、`user_trading.py`。公共 broker 原有共享 bytes（`realtime.py:128–133`）、公共 snapshot anchor 与 history 读取/锁边界（`fx_stream.py:71–105`）均无改动；writer、资金、风控及公共 history cursor/FOR SHARE 路径无 diff。
- **测试适配保留有效断言。** `test_account_events.py:11–24` 只在已有私有业务 fixture 消费时解码，使用方实际只需 get_nowait/empty，账户业务断言没有删除。`test_trading_context.py:148–164` 将解码后已不成立的 dict 对象身份检查改为值相等，同时保留 model_dump 次数、依赖过滤和变更断言，并在 `144、157、173` 直接断言共享 wire 身份。`test_trading_stream.py:143–148` 保留完整行情输入和 stamp 断言，补足真实 ASGI pricing → account_changed → fx_order_result 顺序；原 sync、认证、慢队列、到期及持续队列下 15 秒检查用例未削弱。

## 验证证据与边界

按任务约束，本审阅未重复运行测试，未启动服务、PG、负载或浏览器。检查方式为读取固定 diff、带行号源码、调用搜索和本地 Starlette 发送实现。产品代码、index、HEAD、分支及主 agent 未提交计划均未修改；仅写本报告。

实施报告记录：相关五文件测试 **66 passed**；最终三个直接 SSE 文件及受共享 fixture 影响的代表性业务测试 **23 passed**；`git diff --check` exit 0。这些是实施者提供的执行证据，本审阅不将其表述为独立复跑。

必要但尚未证实：统一环境下实际性能收益，留给主 agent 原定性能对照；本轮不要求额外测试或实现。

## 未纳入本轮裁决的行为

- writer、资金、风控及其 SQL 优化：明确超出此次三文件 SSE 复用授权；未据此要求修改。
- 任意非生产调用向 offer 传入不可 JSON 序列化对象：实际生产 payload 已核对；未将假设输入扩成新的防御框架或产品需求。
- 全仓、PG 与浏览器验收：用户明确限制此次验证范围，本轮只评价本提交与已给出的相关验证证据。
