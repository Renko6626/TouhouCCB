# FX 高负载诊断：市场交易串行路径与重复读取

本轮仅诊断，未修改仓库业务代码。沿用 bde43b0 后端快照加已确认的短认证 session 修复、项目 backend/venv、真实 PostgreSQL 14、单 Uvicorn worker、应用池 10+20、100 个账户和 200 条 SSE。每轮新建隔离库，未访问生产环境。

## 主要发现

直接可观测的长等待发生于市场交易门闩。高压时市场门闩排队峰值 102 个任务，而池 checkout 采样峰值 20/30；PG 数据库锁等待采样峰值仅 1。应用门闩与数据库锁是不同的等待位置。

| 较轻采样阶段 | 门闩等待平均/P99 | 单次占用门闩平均/P99 | 门闩排队峰值 | 池 checkout 采样峰值 |
| --- | --- | --- | ---: | ---: |
| normal | 46.686/232.24 ms | 52.925/145.526 ms | 4 | 11/30 |
| active | 306.664/1245.143 ms | 81.612/198.935 ms | 11 | 12/30 |
| stress | 6742.65/8828.815 ms | 80.667/321.628 ms | 102 | 20/30 |

门闩计时按每次获取记录，不是每个成功成交只记录一次。融资/空头路径可能先退出门闩发现依赖，再重试获取，因此门闩次数会多于 HTTP 成交请求数。102 是等待任务数，不等于 102 个玩家。取消、客户端超时和服务端完成时点不同。

实际调用链为 `fx/net_trading.py:377 execute_net_trade` → `GATES.hold(exclusive=[fx市场])` → `execute_net_trade_in_session` → 查询并锁定市场/账户/钱包/空头仓位、必要的风控重发现与重验、写成交和账本 → `commit` → 释放门闩。同市场交易按顺序修改储备是既有正确性约束，本轮没有提议移除它。

高压 net/trades 的 `_simulate` 平均 2.744ms（该函数还可能调用风控及等待 IO，不是纯数学 CPU 计时），`check_new_risk` 平均 17.883ms，`discover_dependencies` 平均 29.397ms。它们互相嵌套且可能重复调用，不能把这些平均值或累计时长直接相加，也不能用来宣称某个函数独占的 CPU 百分比。核心执行函数单次平均 71.356ms，持门闩单次平均 80.667ms；长尾主要体现为门闩前的多秒排队。

## 数据库执行放大

下面来自较轻采样 normal 阶段，按真实 HTTP 路径分组计数；SSE 在整个连接存续期间持续查询，因此不是一条 SSE 请求只执行一次。

| 接口 | 请求数 | 数据库执行次数 | 每请求平均次数 |
| --- | ---: | ---: | ---: |
| GET /api/v1/fx/pairs/:id/my-trades | 78 | 156 | 2.0 |
| GET /api/v1/fx/pairs/:id/snapshot | 78 | 234 | 3.0 |
| GET /api/v1/user/trading-context | 78 | 936 | 12.0 |
| GET /api/v1/user/trading-prices | 11 | 55 | 5.0 |
| GET /api/v1/user/trading-versions | 129 | 903 | 7.0 |
| POST /api/v1/fx/pairs/:id/net/quote | 77 | 480 | 6.234 |
| POST /api/v1/fx/pairs/:id/net/trades | 77 | 1787 | 23.208 |

成交后页面刷新包含 trading-context 12 次、snapshot 3 次、my-trades 2 次，合计约 17 次数据库执行，报价、实际成交、版本轮询与 SSE 周期复核另算。net/trades 约 23 次是混合角色/重试下的每 HTTP 请求平均值，不是每一种交易的固定 SQL 数。

`credit/trading_context.py:34 read_dependencies` 分别查询 LMSR 持仓、FX 钱包、FX 空头三组身份。账户 context、versions、prices 和私人 SSE 都会调用它。私人 SSE 每 15 秒复核用户/规则/依赖，在 account_changed 时又读取依赖，见 `api/v1/user_trading.py:101`、`:134` 附近。高压阶段私人 SSE 记录 1790 次数据库执行；账户 context 4332 次、versions 1386 次，实际 net/trades 6459 次。完整分组在 diagnosis.json。

SQL 计数来自 SQLAlchemy cursor 事件，不记录绑定参数。cursor elapsed 包含数据库往返、可能的锁等待和事件循环恢复延迟，不等同于 PostgreSQL 纯执行时间；并发查询累计时长可以大于阶段墙钟时间，不能据此判断数据库 CPU 已满。请求和函数记录按进入时的阶段归属，跨阶段、客户端超时后仍完成的请求存在边界差异。

## CPU 采样与证据限制

外部 py-spy 安装在临时目录，未添加到项目 requirements。首轮 99Hz 阻塞式采样，6264 个样本、0 个采样错误，但出现明显观察扰动：normal 成交 P99 约 2.02 秒，提前停测，未进入高压。该轮不能作为新的容量基准。

随后改为 19Hz 非阻塞式采样，保留原停测保护。短阶段为只读 10 秒、normal/active 各 20 秒、stress 30 秒；正常阶段过短且初始排程更集中，阶段名不代表与五分钟基准完全等价的流量过程。活跃成交 P99 约 2.25 秒、加压 P99 约 14.52 秒；因为活跃阶段少于 30 秒，没有达到原滚动保护的检查时点。高压阶段按原保护自动停止。此轮用于定位，不作为容量验收。

非阻塞采样得到 1120 个样本并报告 757 次采样错误，存在明显样本丢失。两轮叶栈都将 SQLAlchemy 排在主要类别前列（首轮约 40%、次轮约 30%），但采样包含启动/入场且次轮缺样，百分比只作定位线索。不能把它们当成稳定交易阶段的精确 CPU 分账。私人 SSE、公开 SSE、异步调度和数据库访问均可见；没有证据说明单纯 AMM 乘除法占据主要 CPU。

公开行情的 `services/realtime.py` 已将同一 MarketEvent 序列化为共享 bytes 后分发，本轮没有把它误判为每人重复生成整份公共行情。私人 pricing 推送仍在 `trading_prices.py:126` 按订阅者构建 payload，再由每个连接 encode_frame；这是可检查的开销，尚未通过单独关闭推送的对照证明其收益上限。

## 建议的下一步

优先在既有架构内减少重复读取和缩短独占段：先检查账户依赖的三次查询能否合并、context 的 basis 和依赖读取能否避免重复；再核对同一成交执行中已经持锁的数据是否被重复加载，以及能否减少不必要的 flush/数据库往返。每项都应保留认证、版本复核、锁顺序、审计和记账约束，做单项前后对照后再决定保留。

更改前端成交后刷新契约、改变 SSE 规则/封禁检查频率、引入跨请求缓存或调整写入模块边界，会影响语义或架构，应先单独提出方案确认。本轮没有实施这些改动，也没有建议增加 worker 来绕开现有单写所有权。

## 复现与原始证据

- 启动入口：`backend/venv/bin/python /tmp/sunyunbo/fx-pool-loadtest-20261017/run_diagnostic_light.py`。前置需启动该临时 PostgreSQL 集群。
- 临时计时挂钩：同目录 diagnostic_server.py；交易/风控生产逻辑未修改。
- [计时分布](100/timings.json)、[按接口与 SQL 分组统计](100/sql.json)、[服务端请求计时](100/requests-server.json)、[门闩队列采样](100/diagnostic-samples.jsonl)、[数据库与进程采样](100/resources.jsonl)。
- [CPU speedscope 文件](100/cpu.speedscope.json)、[后端及采样日志](100/backend.log)、[HTTP 统计](100/http/summary.json)。
- 首轮高频采样保留于相邻 `../fx-local-diagnosis-20261017/100/`。
- 两轮均未记录 QueuePool 耗尽。临时 token 已删除，临时后端和 PostgreSQL 已停止，调查库仅保留在本机临时目录。
