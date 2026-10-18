# 市场输入内存读取：本机A/B

固定readheavy：目标行情Pair输入1367→0，总SQL8925→7570（约15.2%下降），应用CPU均值39.75%→36.78%（约2.97个百分点）。normal/active成交吞吐近似；stress两版都触发原P99保护并未运行burst，after8.895成交/s低于before9.537。本轮不能认定交易容量改善，也不能认定所有读尾延迟改善；单轮、固定执行顺序及实际请求组成差异，不足以把所有变化确定归因于迁移。

低频固定versions P99 65.74→87.95ms，active context_after_trade 385→947ms、周期versions356→936ms；stress context_after_trade384→7135ms、周期versions400→6947ms。after context共享GATE等待P99约6920ms，表明高压读排队明显加重；不能用SQL减少掩盖读体验回退。公共SSE全轮event-age P99 75.77→42.86ms，私人147.36→160.49ms，亦非所有推送均改善。下一阶段决策点：已确认的账户覆盖sharedGATE在高写入下出现排队，当前证据不能证明SQL/CPU收益足以替代读尾损失；尚未部署，本轮不擅自修改锁或一致性边界。

固定before `a820157e2555e2143d37864962accf74a8f006a0`、after `330f4e683ec5564dea6ea59983351c3c8e128d9d`，均由git archive提取；before已包含融资优化与依赖UNION ALL，after含本次市场提交快照/读取迁移。全backend hash与实际diff见before-source.json、after-source.json、source.diff。性能脚本未改业务源或提交，没有部署；正确性验证由主agent另记validation.md，本harness没有重复PG测试。

## 方法与边界

本机PG14 Unix socket55489、不连接生产；两版分别新建fx_market_memory_{before,after}_r1_test，单Uvicorn worker18543、池10+20，同seed20261017，100用户/200SSE（公开私人各100）、角色现金/融资/空头60/20/20。主agent拥有PG生命周期，本harness结束只关闭自己启动的HTTP/monitor并删除tokens，不关闭cluster。

same轻量SQL计数/分类、GATE、1秒资源、0.5秒池/队列/独立PG活动、eventloop lag与body计数；无CPU profiler。SQLAlchemy before_cursor_execute不覆盖driver pre_ping/控制SQL，不能说全SQL为0。独立PG取queryid/阻塞者、backend CPU、phase边界pg_stat_statements/WAL；WAL是clusterwide，PG累计exec不是CPU时间。

100用户入场/角色准备后，插入readheavy60秒：每用户3轮context/versions/prices/snapshot，计划300次/接口共1200固定读，cap10、轮距20秒/用户错开0.15秒；计划30个低频真实交易循环（18现金6融资6空头，每2秒一次）。随后原readonly30s、normal60s、active45s、stress30s（全体合计25cycles/s）、burst观察10s，条件单跳过。混合阶段已包含readheavy真实交易后的状态，两版相同顺序。脚本/参数/seed相同，不保证异步完成顺序与实际HTTP组成相同；历史补读单列。

保留原30秒滚动错误率>5%或成功成交P99>2000ms保护，readheavy也适用；提前停止不补跑、不放宽。phase级客户端/SQL/GATE汇总含in-flight收尾；资源/PG activity尽量按真实保护epoch截尾，phase观察有约50ms边界误差。保护后SSE尾值0/0来自驱动停止/取消，不能解释为压力下自发掉线；未独立计数取消请求，不能当0，timeout也不能证明未提交。应用CPU100%约一核，不是整机满。

初次before启动发生harness lifespan变量名冲突，负载前即失败；已修复，两版使用相同修正脚本。失败日志保留before-startup-failed，原新建before_test库保留，正式均用_r1_test；该次不计入A/B。

## 读多写少与混合结果

| 版本 | 阶段 | 成交/循环 | 成交/s | HTTP/s | 成交P99 ms | app CPU %（近似） | SQL总数 | 行情Pair输入SELECT（含版本回退） | public共享锁SQL |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| before | readheavy | 30/30 | 0.5 | 22.48 | 87.55 | 39.75 | 8925 | 1367 | 30 |
| before | readonly | 0/0 | 0.0 | 6.451 | — | 16.50 | 1683 | 230 | 0 |
| before | normal | 154/156 | 2.524 | 19.327 | 236.44 | 45.46 | 11741 | 1050 | 154 |
| before | active | 331/338 | 7.124 | 43.089 | 817.66 | 84.90 | 20645 | 1668 | 318 |
| before | stress | 331/390 | 9.537 | 59.64 | 14142.73 | 97.75 | 20030 | 1503 | 239 |
| after | readheavy | 30/30 | 0.499 | 22.473 | 106.44 | 36.78 | 7570 | 0 | 30 |
| after | readonly | 0/0 | 0.0 | 6.451 | — | 15.71 | 1453 | 0 | 0 |
| after | normal | 155/156 | 2.536 | 19.225 | 232.00 | 42.97 | 10684 | 0 | 153 |
| after | active | 333/340 | 7.223 | 42.6 | 955.49 | 82.88 | 18935 | 23 | 324 |
| after | stress | 313/328 | 8.895 | 50.782 | 10396.36 | 97.22 | 16782 | 37 | 245 |

before：停止原因 `30s window: successful trade P99 exceeds 2000ms`；保护记录 `{'t': 1792300233.1371455, 'phase': 'stress', 'reason': '30s window: successful trade P99 exceeds 2000ms'}`；未运行阶段 ['burst']；全轮池峰值24/30，QueuePool错误0、Traceback0、CancelledError文字0。

after：停止原因 `30s window: successful trade P99 exceeds 2000ms`；保护记录 `{'t': 1792300543.831534, 'phase': 'stress', 'reason': '30s window: successful trade P99 exceeds 2000ms'}`；未运行阶段 ['burst']；全轮池峰值23/30，QueuePool错误0、Traceback0、CancelledError文字0。

暖读Pair输入列只统计live_market_read、background:pricing_push、background:public_fx_push的完整/价格输入Pair SELECT；market_data的目录发现/历史维护另列，不包括公共共享锁、业务写/风控/报价、市场目录、个人成交join或history；SQL分类是文本结构启发式，完整route/category仍保留以便核对。公共FOR SHARE从id读取转为pool_version，并没有消除该行锁。after readheavy/readonly/normal的目标行情Pair输入为0；active与stress的public_fx_push分别在324/245次版本guard之后出现23/37次完整Pair回源。read_public_pair_state在锁内发现pool_version与内存peek版本不同时，按设计读取fresh Pair，保证提交与安装内存之间的一致性；这是已设计的fallback，不是0查询路径。普通暖态context/versions/prices/snapshot与pricing_push仍为0，不能将public_fx_push并发回退算成完全消除。账户/依赖/规则SQL与冷态回源允许保留。

| 版本 | 固定接口 | 完成/计划300 | HTTP结果 | P95 ms | P99 ms |
|---|---|---:|---|---:|---:|
| before | fixed_context | 300/300 | {'ok': 300} | 60.27 | 104.95 |
| before | fixed_versions | 300/300 | {'ok': 300} | 31.25 | 65.74 |
| before | fixed_prices | 300/300 | {'ok': 300} | 28.63 | 53.48 |
| before | fixed_snapshot | 300/300 | {'ok': 300} | 20.61 | 27.26 |
| after | fixed_context | 300/300 | {'ok': 300} | 56.27 | 94.92 |
| after | fixed_versions | 300/300 | {'ok': 300} | 35.43 | 87.95 |
| after | fixed_prices | 300/300 | {'ok': 300} | 25.77 | 46.18 |
| after | fixed_snapshot | 300/300 | {'ok': 300} | 20.31 | 24.52 |

固定context/snapshot与交易后的页面刷新复用路由；路由总SQL不能直接除固定300次作为单请求SQL。versions/prices在readheavy没有额外周期reader，route计数可直接核对；依赖SQL两版已经是1条UNION，不把先前3→1再次计为本次收益。

## 交易错误、门闩与连接池

| 版本 | 阶段 | 冲突 | timeout | 业务拒绝 | 报价不可执行 | 净交易GATE等待P99 ms | 账户context共享GATE等待P99 ms | 队列峰 | 池峰 | lag P99 ms |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| before | readheavy | 0 | 0 | 0 | 0 | 0.07 | — | 0 | 8 | 14.31 |
| before | readonly | 0 | 0 | 0 | 0 | — | — | 0 | 4 | 2.37 |
| before | normal | 0 | 0 | 0 | 2 | 36.20 | — | 0 | 11 | 20.64 |
| before | active | 0 | 0 | 0 | 7 | 480.07 | — | 5 | 19 | 83.42 |
| before | stress | 17 | 22 | 1 | 19 | 9506.82 | — | 103 | 24 | 97.41 |
| after | readheavy | 0 | 0 | 0 | 0 | 0.08 | 0.10 | 1 | 7 | 14.27 |
| after | readonly | 0 | 0 | 0 | 0 | — | — | 0 | 4 | 2.57 |
| after | normal | 0 | 0 | 0 | 1 | 44.94 | 57.21 | 2 | 11 | 24.53 |
| after | active | 0 | 0 | 0 | 7 | 675.55 | 769.08 | 17 | 15 | 41.46 |
| after | stress | 0 | 0 | 0 | 15 | 6950.66 | 6919.66 | 143 | 23 | 113.96 |

GATE按每次获取统计，业务可能重试，等待峰值是任务数。账户共享GATE在迁移后参与真实排队，价格缓存命中不代表账户读不会等待写者；各读接口P99与SSE必须一起看，不能只报成交吞吐。

## SSE与HTTP组成

| 版本 | 阶段 | SSE在线公开/私人（阶段尾） | 客户端收到帧公开/私人 | chart/history HTTP分类 |
|---|---|---|---|---|
| before | readheavy | 100/100 | 3000/3430 | [] |
| before | readonly | 100/100 | 0/208 | [] |
| before | normal | 100/100 | 15400/15962 | [] |
| before | active | 100/100 | 31800/33642 | [] |
| before | stress | 0/0 | 23700/26572 | [] |
| after | readheavy | 100/100 | 3000/3430 | [] |
| after | readonly | 100/100 | 0/208 | [] |
| after | normal | 100/100 | 15300/16064 | [] |
| after | active | 100/100 | 32400/33944 | [] |
| after | stress | 0/0 | 24100/27067 | [] |

before 全轮SSE新鲜度（依赖同机wallclock，不能当某一阶段分布）：

public：event age {'count': 82508, 'p50_ms': 23.459, 'p95_ms': 54.204, 'p99_ms': 75.772}；接收间隔 {'count': 82408, 'p50_ms': 130.197, 'p95_ms': 1081.673, 'p99_ms': 2060.735}；counts {'frames': 82508, 'payload_bytes': 48534634}。

private：event age {'count': 88711, 'p50_ms': 22.778, 'p95_ms': 41.932, 'p99_ms': 147.356}；接收间隔 {'count': 88611, 'p50_ms': 123.224, 'p95_ms': 1076.027, 'p99_ms': 2076.412}；counts {'frames': 88711, 'payload_bytes': 43461319}。

before 各阶段单一observer成交可见性见analysis.json trade_visibility；ASGI实际body数/bytes按phase/route另存body-counts.json，可能包含空终止帧，不能等同业务事件数。

after 全轮SSE新鲜度（依赖同机wallclock，不能当某一阶段分布）：

public：event age {'count': 83408, 'p50_ms': 21.306, 'p95_ms': 34.207, 'p99_ms': 42.862}；接收间隔 {'count': 83308, 'p50_ms': 125.098, 'p95_ms': 1085.201, 'p99_ms': 2041.226}；counts {'frames': 83408, 'payload_bytes': 48897284}。

private：event age {'count': 89603, 'p50_ms': 27.731, 'p95_ms': 51.646, 'p99_ms': 160.493}；接收间隔 {'count': 89503, 'p50_ms': 120.343, 'p95_ms': 1013.569, 'p99_ms': 2073.571}；counts {'frames': 89603, 'payload_bytes': 43912767}。

after 各阶段单一observer成交可见性见analysis.json trade_visibility；ASGI实际body数/bytes按phase/route另存body-counts.json，可能包含空终止帧，不能等同业务事件数。

## 市场SQL范围与PG行锁

| 版本 | 阶段 | family | Pair输入SELECT | id共享锁 | pool_version共享锁 | metadata SELECT | volume/history SELECT |
|---|---|---|---:|---:|---:|---:|---:|
| before | readheavy | live_market_read | 1259 | 0 | 0 | 660 | 0 |
| before | readheavy | background:pricing_push | 78 | 0 | 0 | 0 | 0 |
| before | readheavy | background:public_fx_push | 30 | 30 | 0 | 60 | 0 |
| before | readheavy | background:market_data | 0 | 0 | 0 | 30 | 0 |
| before | readheavy | business_quote_or_write | 79 | 0 | 0 | 0 | 33 |
| before | readheavy | personal_trade_join_retained | 0 | 0 | 0 | 0 | 30 |
| before | readonly | live_market_read | 200 | 0 | 0 | 0 | 0 |
| before | readonly | background:pricing_push | 30 | 0 | 0 | 0 | 0 |
| before | readonly | background:market_data | 6 | 0 | 0 | 6 | 0 |
| before | normal | live_market_read | 715 | 0 | 0 | 308 | 0 |
| before | normal | background:pricing_push | 181 | 0 | 0 | 0 | 0 |
| before | normal | background:public_fx_push | 154 | 154 | 0 | 308 | 0 |
| before | normal | background:market_data | 0 | 0 | 0 | 154 | 0 |
| before | normal | business_quote_or_write | 309 | 0 | 0 | 0 | 182 |
| before | normal | personal_trade_join_retained | 0 | 0 | 0 | 0 | 154 |
| before | active | live_market_read | 1002 | 0 | 0 | 662 | 0 |
| before | active | background:pricing_push | 348 | 0 | 0 | 0 | 0 |
| before | active | background:public_fx_push | 318 | 318 | 0 | 636 | 0 |
| before | active | background:market_data | 0 | 0 | 0 | 324 | 0 |
| before | active | business_quote_or_write | 650 | 0 | 0 | 0 | 392 |
| before | active | personal_trade_join_retained | 0 | 0 | 0 | 0 | 331 |
| before | stress | live_market_read | 979 | 0 | 0 | 662 | 0 |
| before | stress | background:pricing_push | 285 | 0 | 0 | 0 | 0 |
| before | stress | background:public_fx_push | 239 | 239 | 0 | 478 | 0 |
| before | stress | background:market_data | 0 | 0 | 0 | 334 | 0 |
| before | stress | business_quote_or_write | 665 | 0 | 0 | 0 | 429 |
| before | stress | personal_trade_join_retained | 0 | 0 | 0 | 0 | 331 |
| after | readheavy | live_market_read | 0 | 0 | 0 | 660 | 0 |
| after | readheavy | background:public_fx_push | 0 | 0 | 30 | 60 | 0 |
| after | readheavy | background:market_data | 0 | 0 | 0 | 30 | 0 |
| after | readheavy | business_quote_or_write | 79 | 0 | 0 | 0 | 33 |
| after | readheavy | personal_trade_join_retained | 0 | 0 | 0 | 0 | 30 |
| after | readonly | live_market_read | 0 | 0 | 0 | 0 | 0 |
| after | readonly | background:market_data | 6 | 0 | 0 | 6 | 0 |
| after | normal | live_market_read | 0 | 0 | 0 | 310 | 0 |
| after | normal | background:public_fx_push | 0 | 0 | 153 | 306 | 0 |
| after | normal | background:market_data | 0 | 0 | 0 | 155 | 0 |
| after | normal | business_quote_or_write | 302 | 0 | 0 | 0 | 183 |
| after | normal | personal_trade_join_retained | 0 | 0 | 0 | 0 | 155 |
| after | active | live_market_read | 0 | 0 | 0 | 666 | 0 |
| after | active | background:public_fx_push | 23 | 0 | 324 | 648 | 0 |
| after | active | background:market_data | 0 | 0 | 0 | 329 | 0 |
| after | active | business_quote_or_write | 634 | 0 | 0 | 0 | 395 |
| after | active | personal_trade_join_retained | 0 | 0 | 0 | 0 | 333 |
| after | stress | live_market_read | 0 | 0 | 0 | 626 | 0 |
| after | stress | background:public_fx_push | 37 | 0 | 245 | 490 | 0 |
| after | stress | background:market_data | 0 | 0 | 0 | 298 | 0 |
| after | stress | business_quote_or_write | 630 | 0 | 0 | 0 | 365 |
| after | stress | personal_trade_join_retained | 0 | 0 | 0 | 0 | 313 |

/fx/pairs目录发现/metadata、本人的my-trades join Pair及实际交易/风控/net/short/borrow报价保留DB；这些SQL不会被作为未达成暖态目标。market_data后台还负责目录id发现、历史/写入，其Pair SELECT结构计数可能包含select(FxPair.id)，不能把该family所有SQL都当当前市场价格输入。此FX场景不证明LMSR容量。

| 版本 | 阶段 | PG blocked backend采样条数 | PG CPU秒（近似） | PGexec累计ms | WALsync ms（clusterwide） |
|---|---|---:|---:|---:|---:|
| before | readheavy | 0 | 2.27 | 450.97 | 49.49 |
| before | readonly | 0 | 0.46 | 65.13 | 0.35 |
| before | normal | 0 | 3.15 | 964.09 | 222.10 |
| before | active | 6 | 6.28 | 4659.14 | 472.61 |
| before | stress | 8 | 5.53 | 6019.34 | 370.76 |
| after | readheavy | 0 | 2.01 | 417.56 | 49.08 |
| after | readonly | 0 | 0.36 | 60.20 | 0.35 |
| after | normal | 0 | 2.80 | 958.93 | 230.18 |
| after | active | 7 | 6.92 | 4181.22 | 465.45 |
| after | stress | 8 | 5.51 | 5220.71 | 371.47 |

0.5s PG活动记录不是锁等待时间占比，短锁等待可能漏采；PG snapshot的实际epoch与queryid/结构SQL、阻塞者保留raw。cursor计数与PG控制SQL不同口径，不能用二者差额推断CPU。没有CPU采样，不作本轮具体函数CPU归因。

## 验证与回退边界

readheavy后保留readonly无成交窗口，观测读取/帧数；本轮短窗口不能自然验证24h成交量过期，主agent在负载结束后另复用现有`python -m pytest -q tests/test_fx_history.py -k volume_24h`，4 passed/25 deselected（0.84s），覆盖滚动窗口边界、外部flush、未ready与空市场；这是测试时钟/夹具证据，不是自然运行24h到期演练，详见主agentvalidation.md。本harness未通过直接修改已提交行时间戳制造缓存不一致。回退为回退本次读迁移代码并重启，无schema回退；本轮未实际演练生产回退、未增设双读机制。

脚本快照harness/与每版actual-command.json、源码hash/diff、客户端http/summary.json/requests.jsonl、sql分类、GATE/队列、资源/lag、SSE body、PG活动/snapshot为证据；固定目录/库名已存在，再跑需新路径/库名，不能开箱重跑。无profiling扰动，但仅单轮固定顺序、实际工作量可能不同，不承诺容量倍数或尾延迟。清理状态见cleanup.json。
