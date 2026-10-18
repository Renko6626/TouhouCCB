# 百人负载 CPU 与数据库事务 profile（2026-10-17）

结论：本轮更支持应用侧SQLAlchemy、异步调度及读取/SSE负载挤占单核，放大交易串行排队。AMM数学没有显示为主要CPU热点，WAL刷盘也没有显示为主要持锁耗时。同时查到公开行情快照与交易行锁的真实等待，不能宣称数据库侧瓶颈全部解决。没有修改业务代码、连接池策略或事件契约。

## 方法与可用范围

与上一轮完全相同的优化后业务快照：短认证session、context复用、显式融资预发现和已锁用户借款都已包含。100用户、200条SSE、60/20/20角色，池10+20，单Uvicorn worker、同机HTTP/PG，PG端口55489仅Unix socket；每组新建独立测试库。正常60秒、活跃45秒、高压30秒，保留30秒滚动错误率/成交P99保护。两组均在活跃阶段约32秒触发P99保护，未进入stress/burst/条件单，不将空的stress采样伪装为高压profile。

control：同样的SQL/门闩/提交/flush/调度计时与PG统计，没有CPU采样。profile：增加49Hz ITIMER_PROF/SIGPROF采样，仅在进程消耗CPU时触发，记录当前执行帧及ContextVar业务路径；沿用async任务上下文区分HTTP/SSE和后台任务。定价publisher、公共publisher、行情运行时另加只在采样组生效的上下文标签，避免greenlet栈截断后丢失业务来源。活跃阶段另用py-spy 0.4.2、19Hz、GIL-only、阻塞式短采20秒交叉核对。采样只涉及临时后端，无业务函数替代。

临时PG启用pg_stat_statements、track_io_timing、track_wal_io_timing，fsync/synchronous_commit均on。按阶段边界记录服务端累计统计差值；约0.5秒记录应用CPU、主线程CPU、该库PG后端CPU、连接及锁等待状态。PG后端CPU按/proc中的进程CPU增量合计，不含数据库监控连接、checkpointer/walwriter或其他库。CPU百分比都按一个核心100%表示。

## 观察扰动与采样质量

| 指标 | 计时控制组 | CPU采样组 |
| --- | ---: | ---: | ---
| normal成交P99 ms | 296.06 | 235.00 |
| active成交P99 ms | 3049.83 | 2394.29 |

进程内CPU采样共2989个tick，活跃1339个，回调错误0。py-spy记录343个样本、0个采样错误（见native.log）；其中7个为空栈，已作为unavailable stack纳入分母，不隐藏。主线程占活跃阶段应用CPU约98.0%，降低了进程CPU定时器混入其他线程CPU的影响。

控制组活跃P99为3.05秒，比上一轮无这些计时挂钩的混合测试1.16秒明显偏高。不能仅凭采样组P99更低就宣称profiler无扰动；顺序运行、流量路径、挂钩、PG统计和外部采样均可能影响结果。本轮用于热点定位，不作为新的容量验收或稳定CPU百分比分账。

## CPU热点：两种方法方向一致

以下为活跃阶段最顶层执行帧所在类别，类别互斥；不是累加各函数inclusive时间。CPU时间采样中C扩展可能在返回到Python后才处理信号，py-spy又仅含持GIL采样；因此只报告主要热点范围，不把小数当成精准成本。

| 类别 | 进程内CPU tick占比 | py-spy样本占比 |
| --- | ---: | ---: |
| SQLAlchemy | 35.5% | 38.8% |
| async scheduling | 34.1% | 30.6% |
| FastAPI/Starlette | 4.6% | 3.2% |
| asyncpg | 1.9% | 2.3% |
| Pydantic | 1.0% | 1.2% |
| JSON | 1.6% | 1.2% |
| AMM math | 0.1% | 0.3% |
| risk Python | 0.0% | 0.3% |
| instrumentation | 0.5% | 2.3% |

SQLAlchemy热点分散在查询结构/参数强制转换（coercions.expect）、cache_key生成、ORM结果装载、事件与greenlet适配，并非只有数据库执行。异步热点包含call_soon、Future/任务/锁、取消处理、socket写入及事件循环回调，不能全归为无用调度。JSON/Pydantic虽有开销，本轮没有证据说明它们是最大部分。py-spy的AMM数学仅1个样本；不将它解读成所有市场/所有仓位规模下数学成本恒定接近零。

## CPU落在哪类业务

ContextVar标签统计包括该路径下的ORM、框架、网络发送等CPU；成交HTTP本身的比例不包含它触发的其他后台任务，不能把它当作一笔交易的全部系统成本。

| 活跃阶段业务路径 | CPU tick占比 |
| --- | ---: |
| GET /api/v1/stream/trading | 27.3% |
| POST /api/v1/fx/pairs/:id/net/trades | 10.5% |
| GET /api/v1/fx/stream/:id | 9.2% |
| GET /api/v1/user/trading-context | 7.8% |
| background | 7.5% |
| background:market_data | 7.4% |
| GET /api/v1/user/trading-versions | 7.0% |
| POST /api/v1/fx/pairs/:id/net/quote | 5.7% |
| background:public_fx_push | 4.1% |
| GET /api/v1/fx/pairs/:id/snapshot | 4.0% |
| GET /api/v1/fx/pairs/:id/my-trades | 3.1% |
| GET /api/v1/user/trading-prices | 2.2% |
| background:pricing_push | 1.9% |
| POST /api/v1/fx/pairs/:id/short/quote | 1.3% |
| POST /api/v1/fx/pairs/:id/short/cover | 1.2% |

私人交易SSE路径约27%，正常阶段也约26%，并非只有停测清理时才出现。其365个活跃tick里204个落在asyncio/anyio、48个在SQLAlchemy；它还包含周期准入/规则复核、account_changed后的依赖刷新、队列等待/取消、断连检查与发送，不能说27%都是JSON序列化。私人pricing后台构造/分发约1.9%；所以单凭“按订阅者model_dump”代码形态，不能认定它是首要热点。

公开行情发送SSE路径约9.2%、公共publisher约4.1%，现有公共payload已共享序列化，这部分CPU还包含分发与200连接中的发送/调度。context、versions、prices、snapshot、my-trades仍占可观比例。只优化成交函数不能自动消除读流量和推送对同一事件循环的争用。

约8.3%的活跃CPU tick发生在当前执行任务持GATE期间，约91.7%在其他执行上下文；前者不等于门闩只被占用8.3%的墙钟时间。门闩持有者等待SQL或恢复运行时，CPU可以被读取或SSE任务占用，而市场锁仍然没有释放。

## 持门闩时长：SQL等待不等于数据库计算

| 活跃阶段指标 | 控制组 | 采样组 |
| --- | ---: | ---: |
| 应用CPU（单核口径） | 90.8% | 88.1% |
| 该库PG后端CPU合计（单核口径） | 16.5% | 16.5% |
| 每次GATE获取平均持有 ms | 90.45 | 90.88 |
| 其中cursor SQL往返累计均值 ms | 72.41 | 72.42 |
| 其中最终提交往返近似均值 ms | 1.65 | 1.85 |
| 余下墙钟时间均值 ms | 16.38 | 16.60 |

同一门闩区间内cursor语句按序执行，因此本表将SQL累计与提交近似从总持有墙钟时间中扣除；余下时间仍包括连接获取/pre-ping、事务开始、ORM处理、Python计算、flush组织和调度等待，不是纯CPU。flush计时包含SQL，commit总计时又可能包含最后flush，均作为嵌套观测保存，不重复相加。GATE按每次获取统计，有债务重试会增加次数，不等于每笔成功成交。

采样组全活跃阶段cursor统计约87.54秒累计等待；PG中相近语句数的SELECT/DML服务端执行累计约5.31秒。两者都是并发请求的累计，不是32秒阶段中的串行时间。PG的约15871次语句平均服务端执行约0.334ms，且包含下节行锁等待；约数不能代表某一条语句的P99。PG指标不含未启用的独立planning时间；客户端指标还含驱动、协议及等待事件循环恢复，阶段边界也约有0.5秒误差。差额不能全部直接算成CPU，但结合应用88%、PG后端16.5%的CPU与热点栈，主要应继续检查应用端。

活跃事件循环延迟P99约91ms，正常约21ms（50ms定时器的额外延迟）；属于与应用争用一致的线索，不足以独自证明每笔SQL延迟由它造成。最终commit近似1.85ms，集群级WAL同步累计约319ms、写入约22.6ms/32秒，读块I/O累计0ms，均未显示刷盘占据持锁的大部分时间。WAL是该临时集群级统计，可能含其他后台活动；不是本接口的独占计时。

## 数据库侧仍有真实读写锁等待

PG最耗时的两类语句是：

- `SELECT fx_pair.id ... FOR SHARE`：209次、累计约3175ms，约15.2ms/次。源码位于backend/app/services/fx/market_data.py:249，公开行情发布在同一读事务内锁住市场行后读取快照。
- `SELECT fx_pair ... FOR UPDATE`：277次、累计约793ms，约2.86ms/次，交易锁市场行。

63个活跃资源采样中有9个观察到Lock等待。FOR SHARE与交易FOR UPDATE会互斥，读快照持锁期间等待应用继续执行，也可能延长后续写入的等待。因此不能把“PG CPU不高”推导成“数据库锁完全无影响”。当前只证明有争用及时间归属线索，未通过关闭/替换该共享锁的对照量化吞吐收益，也没有删除它；该锁承担快照/历史一致性职责。

pg_stat_statements记录还包含大量BEGIN/ROLLBACK。当前SQLAlchemy asyncpg在非autocommit下的pool_pre_ping会创建短事务、发送空查询并rollback（项目venv/sqlalchemy/dialects/postgresql/asyncpg.py:820），短session越多，额外协议/调度往返越多。分析已将事务控制与SELECT/DML的次数分开，避免用廉价BEGIN稀释每条业务SQL的服务端均值。本轮没有单独量化pre-ping成本，没有直接关闭连接健康检查。

## 建议的下一步

1. 优先针对versions、prices、私人SSE中的重复依赖读取，评估在现有语义内减少查询构造和往返；CPU主要热点支持继续减少ORM工作，而非只优化AMM乘除法。
2. 对私人SSE每帧检查、队列/任务/取消及发送做单项临时对照，量化具体环节；保留准入、规则更新、消息顺序及掉线恢复语义，不直接调整复核周期或事件契约。
3. 审查公共行情FOR SHARE的持有范围，区分需要一致读取的部分和锁内额外等待；明确现有一致性目的后再提出改法。
4. 若以上仍不足，单独量化连接pre-ping和短事务协议成本，再决定是否需要配置或连接策略调整。生产设置/重要同步契约变更先展示方案并确认。

本轮没有实施上述优化，也没有新增缓存、队列、状态机或schema。

## 验证、限制与原始证据

两组均按活跃成交P99超过2秒保护停止，未做高压CPU profile；不得把本轮数据当成上一轮25次/s压力阶段的精确分账。控制组没有异常堆栈；采样组停测取消附近有2次连接终止CancelledError堆栈及对应未归还连接的GC告警，已保留在backend.log，另记取消清理问题，未展开修复。两组无QueuePool耗尽文字。SSE结束0/0含驱动主动取消，不能当作自然断流。

完成检查：两组monitor-errors.json均为空、CPU回调错误0、native采样错误0、源码hash与当前工作区4个相关文件一致；临时脚本编译检查通过。未修改业务代码，因此未重复运行单元/全量测试。两组Uvicorn已退出，18543无监听，临时PG已停止，token均删除；未提交、推送或部署。

主要证据：

- analysis.json：阶段计时、PG差值、CPU分类与路径统计。
- profile/cpu.speedscope.json：按阶段汇总的CPU-time采样火焰图数据，合成route根节点便于查看；tick已聚合，不能将其横轴当真实请求时间线。
- profile/native-active.speedscope.json、native.log、native-window.json：外部采样与时间窗口。
- 每版timings.json、sql.json、resources.jsonl、pg-snapshots.jsonl、pg-settings.json、http/summary.json、backend.log：原始计时及数据库证据。
- profile/cpu-samples.json：原始CPU tick、帧表、路径和持GATE标记。

临时入口：`backend/venv/bin/python /tmp/sunyunbo/fx-cpu-io-profile-20261017/run.py`；native_watch.py负责活跃窗口外部采样；analyze.py及report.py汇总。重跑须先启动临时PG并预加载pg_stat_statements，现有输出目录/库名需另选，避免覆盖已有证据。
