# 唯一 writer 高压 profile（2026-10-18）

结论：本场景下，瓶颈不是 AMM 数学或最终风控判定本身。整个应用接近单核饱和，SSE发送与异步调度、SQLAlchemy构造/执行/处理大量小查询占用CPU；writer每笔串行完成多次数据库往返，期间又与这些任务共享事件循环，因此服务时间增长并形成排队。不能把上一轮约88ms执行时间全部解释为writer正在计算。

## 来源与方法

- 固定后端：`6c7675722cda265bd205dced6ce0f939ae7ac769`，与上轮A/B after逐字节相同，后续58101e1只改计划。源码hash见 source-hashes.json；实验只增加harness，没有修改产品文件。
- 一轮100用户、200 SSE（公共/私人各100）、cash/finance/short=60/20/20，沿用上轮所有初始化、读取密集/只读/常态/活跃/压力时序与原保护。单交易目标FX市场，不能外推复杂多市场组合的风险计算成本。实验源码沿用每topic/IP200额度；主工作区原10额度没有改变。
- 隔离库 `fx_writer_profile_20261018_test`，PG14本地Unix socket55489；fsync/synchronous_commit开启，pg_stat_statements与IO/WAL计时沿用。未接触生产数据库。
- active/stress启用49Hz SIGPROF，按当前route/span标记CPU栈；同时包装真实函数、AsyncSession.execute/flush/commit，计时包含await等待。stress另用19Hz `py-spy --gil --native` 采样。
- profile只定位热点。嵌套span不能相加；SQL cursor elapsed包含网络、调度和可能锁等待，不是数据库纯执行时间。跨await的process_time差值没有被冒充单请求CPU。

实际执行：

```bash
backend/venv/bin/python -m py_compile /tmp/sunyunbo/fx-writer-profile-20261018/profile_server.py /tmp/sunyunbo/fx-writer-profile-20261018/run.py
backend/venv/bin/python /tmp/sunyunbo/fx-writer-profile-20261018/run.py --start-authorized
backend/venv/bin/python /tmp/sunyunbo/fx-writer-profile-20261018/analyze.py
```

上述均退出0；driver因保护退出1并非容量通过。源码和全部harness留存，原始指标在 profile/，聚合在 analysis.json。

## 一笔writer操作在哪里耗时

下列为stress启用profile窗口内的真实调用均值；各行是不同调用集合，并有包含关系，不能相加当一笔总时间。

|观测|调用数|均值|含义|
|---|---:|---:|---|
|成功NetTrade execute_command|215|127.61ms|整个handler，含关闭session、提交和发布等|
|其中SQL cursor elapsed|同215笔|69.12ms/笔|每笔平均17.60条SQL，含等待/调度，已包含flush中的SQL|
|execute_net_trade_in_session|215|96.27ms|锁、身份/幂等、账户准备、模拟、写账，包含下列操作|
|discover_dependencies|70|21.22ms/次|锁后读取本人完整账本/利率并捕获行情；其余无需求路径不调用|
|_simulate|216|0.20ms/次|包含净交易数学、模拟和必要风控，含1笔业务拒绝路径|
|check_new_risk|48|0.15ms/次|最终风险判定；不包含事先准备账户数据的成本|
|_collateral_value|48|0.05ms/次|本场景组合估值，嵌套在风险检查内|
|_write_spot_ledger|197|17.32ms/次|写现货账本，包含内部flush|
|最终AsyncSession.flush|215|8.28ms/次|execute_command直接调用，不含所有内层flush|
|最终AsyncSession.commit|215|2.65ms/次|客户端提交elapsed，不能与数据库纯执行混同|

成功ShortCover有21个窗口内操作，handler均值141.40ms，每笔平均18.33条SQL、cursor elapsed79.30ms。

active阶段不运行外部native profiler：296笔成功NetTrade handler均值79.86ms、cursor elapsed37.44ms、平均17.54条SQL；_simulate均值0.22ms，check_new_risk均值0.15ms。两阶段都指向“账户/事务准备与往返远大于纯数学”，不是只看stress一个数字。

这里的风控必须拆成两件事：获取本人真实资金/债务/持仓需要数据库工作；拿到输入后计算是否允许交易很快。优化前者不能跳过账户锁或沿用排队前的旧账本。

## 整个进程的CPU花在哪里

SIGPROF active1813样本、stress1282样本，采样器错误0。下表是按当前执行上下文的归属；代表样本占比，并非精确CPU账单，也不是HTTP请求的walltime。

|执行上下文|active占比|stress占比|
|---|---:|---:|
|私人SSE /stream/trading|29.2%|26.2%|
|公共FX SSE|9.9%|8.6%|
|FX writer consumer|9.7%|11.2%|
|净交易quote HTTP|6.0%|8.4%|
|账户context HTTP|6.5%|8.1%|
|账户versions HTTP|5.6%|4.4%|
|行情历史聚合background:market_data|7.9%|5.1%|
|公共帧准备background:public_fx_push|4.7%|5.1%|

writer并未独占整个核心。SSE与账户/报价读取同样消耗这个进程的CPU，writer在等待SQL结果后，也要等事件循环调度才能继续。

从另一角度按采样栈最内层Python文件分类，stress约30.3%落在SQLAlchemy、26.4%在asyncio、8.6%在AnyIO；这与上面的route是两种重叠分类，不能相加。SQLAlchemy热点包括SQL cache-key构造、ORM/SQL表达式、greenlet桥接；asyncio/AnyIO热点包括socket write、callback调度、queue等待及取消清理。JSON编码最内层样本仅约1.6%，不能把SSE成本全解释为JSON序列化。

代码核对支持这些观测：

- `PricingPublisher.run` 已共享读取、共享group的model_dump；随后仍按订阅者offer消息。
- 私人SSE每帧仍执行断连检测、wait_for/queue处理、编码和网络发送；account_changed还会重新查询依赖。
- 公共SSE每次循环创建queue/kicked两个Task并清理等待；200条连接在大量消息下带来调度和对象成本。
- 用户context仍需权威版本前后检查及本人账本SQL；交易quote也有账户查询。行情内存化没有消除这些工作。

stress服务器phase记录约24141个私人SSE body帧、21300个公共SSE body帧（包含phase尾部，和CPU采样窗口不完全相同，不能直接混除）。这说明一次市场更新会放大为许多连接上的消息处理。

native补充：py-spy记录437个样本，76次采样错误，权重合计约23秒；`gc_collect_main`最内层权重约1.84秒，占有效权重8.0%。这支持存在对象分配/GC成本，但native仅采持GIL线程、可暂停进程且有缺失，不据此修改GC配置。49Hz信号也会延迟交付，C层成本需结合native看，不能把采样百分比当精确归因。

## 数据库观测

PG stress入口到保护快照间约30.57秒，各SQL累计server execution约4.26秒；这不是墙钟关键路径，也未包含commit全部成本。约2.65秒累计在公共history读取的pool_version FOR SHARE，目标Pair FOR UPDATE累计约0.276秒。server执行时间可以包含锁等待，不能直接当CPU。

因此数据库仍参与每笔路径，公共精确history仍会等待写锁；但没有证据表明WAL提交吞吐或AMM数学是当前首要瓶颈。客户端SQL elapsed大于SQL实际server execution，包含了调度、结果处理等成本。不能简单扩连接池来解释或解决这些耗时。

## profiler扰动与错误

- active成功吞吐7.046/s，上一轮无profile为7.066/s；stress本轮8.051/s，上一轮10.909/s。
- 本轮stress共281个成功HTTP成交响应、74个503、1个业务409；74个503均为 `FX operation result unknown; refresh to confirm`，约10秒，符合writer submit超时。结果未知不能当作已回滚，也不能把它们算成功响应吞吐。
- 触发原“30秒错误率>5%”保护，burst未执行。上一轮无profile触发的是成功交易P99>2秒。profiling、native暂停与观测开销会扰动压力下的队列，未做新control轮，不能把本轮8.051/s当生产容量或新性能回退结论。
- stress成功handler127.61ms高于上一轮约87.78ms，边界也略有差别（本轮handler vs上轮consumer整段）。这些数值只用于定位，不替代上一轮未开启profiler的容量结果。
- 保护发生后停止开启新profile span；已开始的span会完成记录，之后仍可能有队列排空。因此window内215笔NetTrade/21笔ShortCover不等于整个stress成功HTTP281笔。

## 后续优化顺序建议（尚未实施）

1. 先检查推送的逐订阅者调度成本与重复发送，尤其普通私人行情/公共行情是否能减少相同价格的重复处理、复用编码。任何合并/降频须先明确业务需要保留哪些事件，不能丢资金/订单事件或破坏历史游标。
2. 审计writer和quote内多次账户/配置查询，复用同一锁后已加载数据，减少SQLAlchemy往返和重复flush；仍保留真实User锁、最新本人账本、同事务资金/流水和幂等。
3. 对ctx/versions等读取的重复配置/SQL工作按真实热点处理。此次没有证据支持优先重写AMM算法、削弱风控或继续扩大连接池。

本次仅profile，无产品逻辑更改，无永久测试新增；未跑浏览器或全仓测试。实验服务/driver/PG monitor/native profiler均已退出，临时token删除，独立PG由主控制器fast stop成功。现有脏工作区修改未动。
