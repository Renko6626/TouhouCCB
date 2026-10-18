# 100用户高压诊断：CPU与等待时间

直接排队点是同一FX品种的写门闩：control完整窗口事件平均等候6.35s、持有87.28ms，等待任务峰101；profile平均等候7.34s、持有100.16ms，峰108。两轮持有时间总和约29.3/29.5s，接近各自30秒观察窗口，写临界区持续繁忙；池未耗尽，不能把连接池作为本轮首要队列。profile应用CPU约88.95%，control约97.56%；blocking外部采样会暂停目标，同时实际完成工作量也不同，不能把CPU比例下降理解为应用变轻。profile仪器有可见干扰；实际历史补读分支也不同：control stress汇总chart_tail292次、history_segment72次，profile均0（含drain口径）。两轮吞吐差额不能用于量化仪器损耗，以上不构成新容量结论。本轮目标是采到真实100人高压并定位，未为精确估计profiler开销补跑。

CPU没有集中到一个业务算法：严格stress1289个SIGPROF ticks中，SQLAlchemy叶帧34.83%、async调度34.21%，分散在缓存键生成、greenlet边界、调度/取消/transport写等函数；外部native421样本中SQLAlchemy24.94%、async调度26.60%，另gc_collect_main为39样本（9.26%）。外部89 errors且blocking会暂停应用，精确比例不可靠，但两类采样都支持框架/任务/对象处理广泛占用，未支持AMM算术为主热点。

等待时间也不是纯数据库执行：profile完整持有事件均值100.16ms，cursor elapsed79.22ms、commit-driver2.40ms、其余18.54ms。cursor包含PG、驱动及恢复调度，不能按79ms全算数据库，更不能把其与PGexec差额全算CPU。PG确有FOR SHARE/UPDATE锁等待：control/profile分别采到13/9条blocked backend记录，FOR SHARE累计约3738/3630ms；WAL sync累计373/336ms，读写I/O计时均0（缓存热的本轮，不能外推冷盘）。control两次FOR UPDATE受阻采样中，阻塞者均为idle in transaction/ClientRead，最后query分别在market metadata与rolling24h volume读取；这直接支持读事务可能阻写。13/59次采样见阻塞不是锁等待时间占比。较多idle in transaction/ClientRead说明应用持事务时DB在等客户端；观测到的事实是写门闩持续繁忙、CPU接近单核且分散、存在DB锁阻塞；由此推断应用侧CPU竞争/恢复调度与事务锁持有共同放大临界区elapsed，具体各自份额本轮不能完全分离，也未通过开关实验验证独立因果。

私人SSE synthetic标签占25.60% ticks，其中188/330个叶样本属async调度；公共SSE9.23%，两条SSE合计约34.8%。本窗口ASGI body消息私人24353、公共22000，确有每次成交后面向百个订阅者的调度/send放大。不能由这些数字直接说JSON是主因：JSON叶帧仅1.86%，pricing_push标签1.40%，未知/native与GC仍存在归因边界。39个gc_collect_main样本的Python父链人工复核中，至少8个经过private frames→is_disconnected→receive_or_disconnect→anyio.__aexit__，至少5个经过该接收链→start_soon→_spawn；这支持任务/取消检查相关分配与GC的线索，不能当作严格独立成本或把39个GC样本全部归给这条链。

本轮只诊断，不实现优化，内存迁移保持暂停。实验基线HEAD `d88a9399ded066eb517d7eee5fba8b43fffd886a`，包含融资4文件未提交改动及已提交query优化；control/profile全backend源码hash一致，见source-hashes.json。仅本机隔离PG14 Unix socket55489、每轮新建fx_stress_profile_{control,profile}_test；单Uvicorn worker，127.0.0.1:18543，池10+20。未连接生产、未修改业务源、未提交或部署。

## 方法及窗口

两轮复用相同负载脚本、参数与seed，实际HTTP组成不同（历史补读/SSE-tail动态分支、异步完成顺序）；不声称逐笔请求完全相同：100用户、公开/私人SSE各100，现金/融资/空头=60/20/20，seed20261017。smoke10s、每用户入场0.3s、readonly30s、normal60s、active45s、stress30s，stress需求为全体合计25交易循环/s。保持原30s滚动错误率>5%或成功成交P99>2000ms保护；不补跑、不放宽阈值。

两轮stress前均只有同样的SQL执行次数、GATE等候/持有、池峰值、事件循环lag与资源轻量监测。control不启CPU/重型SQLelapsed；profile在server观察到真实stress后启用49Hz SIGPROF、cursor/flush/commit/函数elapsed、pool._do_get计时，同时外部py-spy19Hz --gil --native。两轮性能差异包含全部这些仪器开销，不能全归于CPU采样。独立PG进程不依赖应用event loop，0.5s采集本库pg_stat_activity、queryid、阻塞者与backend CPU，stress边界取pg_stat_statements/WAL快照。

CPU每tick只在内存记录栈、epoch、route与当前task是否实际持GATE，无文件IO；route/background是synthetic上下文标签，不是完整async调用因果。SIGPROF按进程CPU计时但Python signal可能延后处理、合并；C扩展中的CPU可能记到返回后的Python位置，具体归因需结合外部样本。CPU原始tick按客户端stress开始→保护触发epoch裁剪，shutdown/drain排除。GATE分解只统计requested/acquired/released全部落在窗口的事件；跨界/收尾另列，长等待的删失会使完整事件分位数低估末尾排队，因此同时列原始队列增长。

应用CPU100%约为单worker占一个核，不是整机耗尽。profile窗口主线程CPU占进程CPU约97.06%，支持主线程样本代表主要进程工作；其余线程CPU仍可能通过进程SIGPROF带来小偏差。事件循环lag（超出50ms tick的延迟）control均值9.13ms/P99 138.85ms/max183.80ms，profile均值11.03ms/P99 132.10ms/max176.39ms。host-start/end记录112逻辑CPU、affinity、cgroup配额/节流与CPU pressure/load；应用、数据库、驱动同机，运行顺序固定、未重复交叉。

## 场景结果

| 轮次 | stress窗口s | 循环启动数 | 报价HTTP完成 | 完成写请求 | 成功 | 冲突 | timeout | 业务拒绝 | 报价不可执行 | 成交/s（含drain阶段） | 成功P99 ms（含drain） |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| control | 30.10 | 393 | 393 | 381 | 335 | 21 | 24 | 1 | 12 | 9.772 | 14473.511 |
| profile | 30.52 | 354 | 354 | 348 | 292 | 27 | 29 | 0 | 6 | 8.446 | 13910.501 |

control：保护原因 `30s window: successful trade P99 exceeds 2000ms`；真实stress开始 1792296077.7276428，停止负载 1792296107.8261807，stage返回 {'t': 1792296112.0103526}；全轮池峰值18/30。未运行阶段：['burst']。

profile：保护原因 `30s window: successful trade P99 exceeds 2000ms`；真实stress开始 1792296326.4573796，停止负载 1792296356.9733882，stage返回 {'t': 1792296361.0314999}；全轮池峰值23/30。未运行阶段：['burst']。

循环含报价与页面读，不等于写请求；报价HTTP成功仍可能executable=false。以上客户端stress汇总包含保护后的in-flight drain，不能与严格裁剪CPU/PG窗口直接相除。驱动停止结束200条SSE后尾值0/0，不能解释为负载中自发掉线；HTTP/task取消未单独计数，不能记作零。timeout不能证明服务端未提交。

## CPU采样与路由

| 轮次 | 应用CPU秒（采样近似） | 应用CPU % | PG后端CPU秒（近似） | CPU ticks | pool峰值 | GATE等待峰值 |
|---|---:|---:|---:|---:|---:|---:|
| control | 29.51 | 97.56 | 6.12 | 0 | 18 | 101 |
| profile | 26.89 | 88.95 | 5.26 | 1289 | 23 | 108 |

profile SIGPROF errors=0，窗口外tick排除=0。下面是叶帧样本比例；不等于独立函数精确CPU成本。

| 叶帧类别 | 样本 | % |
|---|---:|---:|
| SQLAlchemy | 449 | 34.83 |
| async scheduling | 441 | 34.21 |
| other Python/native caller | 214 | 16.60 |
| FastAPI/Starlette | 54 | 4.19 |
| other application Python | 33 | 2.56 |
| asyncpg | 28 | 2.17 |
| JSON | 24 | 1.86 |
| Pydantic | 21 | 1.63 |
| instrumentation | 20 | 1.55 |
| Uvicorn | 4 | 0.31 |
| risk Python | 1 | 0.08 |

| synthetic route/background | 样本 | % |
|---|---:|---:|
| GET /api/v1/stream/trading | 330 | 25.60 |
| POST /api/v1/fx/pairs/:id/net/trades | 170 | 13.19 |
| GET /api/v1/fx/stream/:id | 119 | 9.23 |
| GET /api/v1/user/trading-context | 113 | 8.77 |
| background | 112 | 8.69 |
| background:market_data | 92 | 7.14 |
| POST /api/v1/fx/pairs/:id/net/quote | 86 | 6.67 |
| GET /api/v1/user/trading-versions | 62 | 4.81 |
| GET /api/v1/fx/pairs/:id/snapshot | 58 | 4.50 |
| background:public_fx_push | 49 | 3.80 |
| GET /api/v1/fx/pairs/:id/my-trades | 47 | 3.65 |
| GET /api/v1/user/trading-prices | 19 | 1.47 |
| background:pricing_push | 18 | 1.40 |
| POST /api/v1/fx/pairs/:id/short/quote | 9 | 0.70 |
| POST /api/v1/fx/pairs/:id/short/cover | 5 | 0.39 |

当前task持GATE样本分布：`[{'name': 'False', 'samples': 1166, 'percent': 90.45771916214119}, {'name': 'True', 'samples': 123, 'percent': 9.542280837858804}]`。

| 高占用叶函数 | 样本 | % |
|---|---:|---:|
| /data/sunyunbo/miniconda3/lib/python3.13/asyncio/selector_events.py:write | 70 | 5.43 |
| /data/sunyunbo/miniconda3/lib/python3.13/asyncio/events.py:_run | 48 | 3.72 |
| /data/sunyunbo/miniconda3/lib/python3.13/_weakrefset.py:_remove | 39 | 3.03 |
| /data/sunyunbo/miniconda3/lib/python3.13/asyncio/base_events.py:call_soon | 32 | 2.48 |
| /data/sunyunbo/www/TouhouCCB/backend/venv/lib/python3.13/site-packages/sqlalchemy/sql/cache_key.py:_gen_cache_key | 28 | 2.17 |
| /data/sunyunbo/www/TouhouCCB/backend/venv/lib/python3.13/site-packages/sqlalchemy/util/_concurrency_py3k.py:greenlet_spawn | 28 | 2.17 |
| /data/sunyunbo/www/TouhouCCB/backend/venv/lib/python3.13/site-packages/sqlalchemy/util/_concurrency_py3k.py:await_only | 20 | 1.55 |
| /data/sunyunbo/miniconda3/lib/python3.13/asyncio/selector_events.py:_read_ready__data_received | 17 | 1.32 |
| /data/sunyunbo/www/TouhouCCB/backend/venv/lib/python3.13/site-packages/sqlalchemy/dialects/postgresql/asyncpg.py:_prepare_and_execute | 16 | 1.24 |
| /data/sunyunbo/www/TouhouCCB/backend/venv/lib/python3.13/site-packages/sniffio/_impl.py:current_async_library | 15 | 1.16 |
| /data/sunyunbo/www/TouhouCCB/backend/venv/lib/python3.13/site-packages/sqlalchemy/sql/coercions.py:expect | 15 | 1.16 |
| /data/sunyunbo/miniconda3/lib/python3.13/asyncio/base_events.py:_call_soon | 14 | 1.09 |
| /data/sunyunbo/miniconda3/lib/python3.13/asyncio/locks.py:wait | 14 | 1.09 |
| /data/sunyunbo/www/TouhouCCB/backend/venv/lib/python3.13/site-packages/anyio/_backends/_asyncio.py:_deliver_cancellation | 14 | 1.09 |
| /data/sunyunbo/miniconda3/lib/python3.13/asyncio/base_events.py:get_debug | 14 | 1.09 |
| /data/sunyunbo/www/TouhouCCB/backend/venv/lib/python3.13/site-packages/sqlalchemy/event/base.py:__getattr__ | 13 | 1.01 |
| /data/sunyunbo/miniconda3/lib/python3.13/json/encoder.py:iterencode | 12 | 0.93 |
| /data/sunyunbo/www/TouhouCCB/backend/venv/lib/python3.13/site-packages/sqlalchemy/util/langhelpers.py:__get__ | 11 | 0.85 |

profile主线程CPU/进程CPU比例（近似）：0.9705518355249438。

外部采样：421 samples（日志报告89 errors，缺失/错误样本不作为空闲证据）；实际flags为 --rate19 --gil --native，blocking=True，退出status=0。进程时窗 1792296326.4701676→1792296356.976876（非逐tick精确时间戳，可能含启动开销/边界几十毫秒）。原始日志：

```text
py-spy> Sampling process 19 times a second. Press Control-C to exit.


py-spy> Stopped sampling because Control-C pressed
py-spy> Wrote speedscope file to '/data/sunyunbo/www/TouhouCCB/output/fx-stress-profile-20261018/profile/native-stress.speedscope.json'. Samples: 421 Errors: 89
py-spy> Visit https://www.speedscope.app/ to view
```

| 外部叶帧类别 | 样本 | % |
|---|---:|---:|
| other Python/native caller | 138 | 32.78 |
| async scheduling | 112 | 26.60 |
| SQLAlchemy | 105 | 24.94 |
| FastAPI/Starlette | 20 | 4.75 |
| asyncpg | 18 | 4.28 |
| other application Python | 13 | 3.09 |
| instrumentation | 6 | 1.43 |
| Uvicorn | 4 | 0.95 |
| Pydantic | 2 | 0.48 |
| JSON | 2 | 0.48 |
| risk Python | 1 | 0.24 |

## GATE、连接池与事件循环

GATE每次获取事件均有requested/acquired/released时间戳。持有内cursor时间包括驱动等待、PG执行、网络和协程重新获调度等；commit-driver从SQLAlchemy commit开始到after_commit，含驱动和调度；remaining=held-cursor-commit，包含Python、pool pre_ping/获取及其他await/调度，不能称为纯CPU。flush inclusive与cursor重叠，不能再相加。pool._do_get是实际获取路径的elapsed，含连接创建/调度，且不包含随后pool_pre_ping，因此不是纯连接池队列等待。

| 轮次 | 完整/记录GATE | 等候P99 ms | 持有均值ms | cursor均值ms | commit均值ms | remaining均值ms |
|---|---:|---:|---:|---:|---:|---:|
| control | 336/432 | 8517.50 | 87.28 | — | — | — |
| profile | 295/390 | 10680.69 | 100.16 | 79.22 | 2.40 | 18.54 |

control event_loop_lag：background n=508 mean=9.13ms P99=138.85ms

control 保护后观察：`{'observed_seconds': 4.281497001647949, 'first_zero_queue_and_holders_after_stop_s': 3.269094467163086, 'natural_recovery_claim': False, 'reason': 'driver stop and task cancellation/phase drain; not a natural unconstrained recovery experiment'}`。此为驱动停止/取消后的收尾，不构成自然恢复时间。

profile pool_do_get_including_creation_scheduling：POST /api/v1/fx/pairs/:id/net/quote n=336 mean=2.47ms P99=69.49ms；POST /api/v1/fx/pairs/:id/net/trades n=652 mean=0.55ms P99=22.12ms；GET /api/v1/stream/trading n=453 mean=0.28ms P99=0.01ms；background:pricing_push n=249 mean=0.53ms P99=0.00ms；background:market_data n=893 mean=0.27ms P99=0.00ms；GET /api/v1/user/trading-context n=1040 mean=0.20ms P99=0.01ms；GET /api/v1/fx/pairs/:id/snapshot n=227 mean=0.77ms P99=0.01ms；GET /api/v1/fx/pairs/:id/my-trades n=227 mean=1.90ms P99=54.44ms；GET /api/v1/user/trading-versions n=594 mean=0.61ms P99=0.01ms；background:public_fx_push n=501 mean=0.22ms P99=0.01ms；GET /api/v1/user/trading-prices n=213 mean=0.64ms P99=0.01ms；POST /api/v1/fx/pairs/:id/short/quote n=18 mean=2.20ms P99=0.00ms；POST /api/v1/fx/pairs/:id/short/cover n=34 mean=0.00ms P99=0.00ms；background n=10 mean=0.00ms P99=0.00ms

profile event_loop_lag：background n=499 mean=11.03ms P99=132.10ms

profile flush_inclusive：POST /api/v1/fx/pairs/:id/net/trades n=742 mean=10.46ms P99=60.80ms；background:market_data n=231 mean=3.99ms P99=21.74ms；POST /api/v1/fx/pairs/:id/short/cover n=32 mean=19.58ms P99=83.31ms

profile commit_including_flush：POST /api/v1/fx/pairs/:id/net/trades n=234 mean=13.86ms P99=64.33ms；background:public_fx_push n=221 mean=5.87ms P99=29.42ms；POST /api/v1/fx/pairs/:id/short/cover n=16 mean=9.42ms P99=24.19ms

profile discover_dependencies：POST /api/v1/fx/pairs/:id/net/quote n=61 mean=36.27ms P99=144.68ms；POST /api/v1/fx/pairs/:id/net/trades n=114 mean=30.74ms P99=93.43ms；POST /api/v1/fx/pairs/:id/short/quote n=18 mean=22.61ms P99=58.85ms；POST /api/v1/fx/pairs/:id/short/cover n=18 mean=30.51ms P99=64.77ms

profile check_new_risk：POST /api/v1/fx/pairs/:id/net/quote n=61 mean=34.58ms P99=123.06ms；POST /api/v1/fx/pairs/:id/net/trades n=36 mean=17.95ms P99=48.48ms；POST /api/v1/fx/pairs/:id/short/quote n=18 mean=15.30ms P99=33.55ms

profile _simulate：POST /api/v1/fx/pairs/:id/net/quote n=336 mean=13.02ms P99=153.09ms；POST /api/v1/fx/pairs/:id/net/trades n=234 mean=2.90ms P99=44.59ms

profile 保护后观察：`{'observed_seconds': 4.236637592315674, 'first_zero_queue_and_holders_after_stop_s': 3.2193896770477295, 'natural_recovery_claim': False, 'reason': 'driver stop and task cancellation/phase drain; not a natural unconstrained recovery experiment'}`。此为驱动停止/取消后的收尾，不构成自然恢复时间。

## PostgreSQL执行、I/O与控制语句

PG语句累计elapsed可跨连接重叠，不是CPU秒；与cursor合计的差额也不是CPU。SQLAlchemy cursor hook不包含asyncpg pool_pre_ping的BEGIN/ping/ROLLBACK等driver内部控制语句；PG端差分包含这类语句。PG_STAT快照由独立进程观察phase/protection后触发，实际窗口延迟下列列出，in-flight未完成语句/统计刷新可能落在收尾快照。pg_stat_activity是0.5s采样，短等待可能漏采。WAL为clusterwide（本次临时cluster，无生产），不强行归因每个用户事务。

| 轮次 | PGexec累计ms | 读I/O ms | 写I/O ms | WALsync ms | WALwrite ms | blocked backend采样数 | 边界延迟entry/end ms |
|---|---:|---:|---:|---:|---:|---:|---|
| control | 6680.14 | 0.00 | 0.00 | 373.27 | 22.54 | 13 | 73.19/46.89 |
| profile | 6231.85 | 0.00 | 0.00 | 335.74 | 21.24 | 9 | 49.26/58.30 |

control pg_stat_activity：`[{'state': 'idle', 'type': 'Client', 'event': 'ClientRead', 'samples': 449}, {'state': 'idle in transaction', 'type': 'Client', 'event': 'ClientRead', 'samples': 259}, {'state': 'active', 'type': 'Client', 'event': 'ClientRead', 'samples': 19}, {'state': 'active', 'type': 'Lock', 'event': 'transactionid', 'samples': 13}, {'state': 'active', 'type': None, 'event': None, 'samples': 9}, {'state': 'idle in transaction', 'type': None, 'event': None, 'samples': 5}, {'state': 'active', 'type': 'IO', 'event': 'WALSync', 'samples': 2}]`。

| calls | PGexec ms | query结构（无参数） |
|---:|---:|---|
| 250 | 3737.70 | `SELECT fx_pair.id  FROM fx_pair  WHERE fx_pair.id = $1::INTEGER FOR SHARE` |
| 337 | 1211.30 | `SELECT fx_pair.id, fx_pair.currency_code, fx_pair.currency_name, fx_pair.status, fx_pair.archived, fx_pair.gold_reserve, fx_pair.foreign_reserve, fx_pair.initial_price, fx_pair.buy_fee_ … eated_at, fx_pair.updated_at  FROM fx_pair  WHERE fx_pair.id = $1::INTEGER FOR UPDATE` |
| 512 | 268.96 | `SELECT (SELECT fx_market_data_state.history_ready  FROM fx_market_data_state  WHERE fx_market_data_state.pair_id = $1::INTEGER) AS anon_1, (SELECT coalesce(sum(fx_candle.gold_volume), $ … M fx_market_data_state  WHERE fx_market_data_state.pair_id = $21::INTEGER)) AS anon_5` |
| 251 | 199.10 | `INSERT INTO fx_candle (pair_id, interval, bucket_start, open_price, high_price, low_price, close_price, gold_volume, n_trades, first_event_at, first_event_id, last_event_at, last_event_ … event_id END, n_events = (fx_candle.n_events + excluded.n_events), updated_at = now()` |
| 288 | 167.47 | `INSERT INTO fx_trade (execution_id, execution_leg, pair_id, user_id, purpose, requested_foreign_amount, borrow_amount, max_gold_in, cover_all, side, input_amount, output_amount, min_out …  6), $20::VARCHAR, $21::VARCHAR, $22::TIMESTAMP WITH TIME ZONE) RETURNING fx_trade.id` |
| 288 | 131.31 | `INSERT INTO fx_pool_event (pair_id, created_at, kind, pre_gold_reserve, pre_foreign_reserve, post_gold_reserve, post_foreign_reserve, gold_volume, trade_id, audit_id) VALUES ($1::INTEGE … ERIC(16, 6), $8::NUMERIC(16, 6), $9::INTEGER, $10::BIGINT) RETURNING fx_pool_event.id` |
| 2185 | 93.28 | `SELECT "user".id AS user_id, "user".casdoor_id AS user_casdoor_id, "user".username AS user_username, "user".email AS user_email, "user".is_active AS user_is_active, "user".is_superuser  … uipped_title_id AS user_equipped_title_id  FROM "user"  WHERE "user".id = $1::INTEGER` |
| 227 | 77.06 | `SELECT fx_pair.id, fx_market_data_state.history_ready, fx_market_data_state.history_version, fx_market_data_state.last_event_id, anon_1.window_max, anon_2.anchor_id, anon_2.kind, anon_2 … ol_event.id DESC   LIMIT $4::INTEGER) AS anon_2 ON $6  WHERE fx_pair.id = $1::INTEGER` |
| 375 | 74.22 | `INSERT INTO audit_event (ts, event_type, user_id, market_id, outcome_id, operator_user_id, ref_table, ref_id, payload, user_after, position_after, market_after) VALUES ($1::TIMESTAMP WI … HAR, $8::INTEGER, $9::JSON, $10::JSON, $11::JSON, $12::JSON) RETURNING audit_event.id` |
| 265 | 62.35 | `INSERT INTO fx_execution (user_id, pair_id, idempotency_key, request_payload, response_version, response_payload, created_at) VALUES ($1::INTEGER, $2::INTEGER, $3::VARCHAR, $4::JSON, $5 … :JSON, $5::INTEGER, $6::JSON, $7::TIMESTAMP WITH TIME ZONE) RETURNING fx_execution.id` |
| 262 | 61.67 | `SELECT fx_trade.id, fx_trade.execution_id, fx_trade.execution_leg, fx_trade.pair_id, fx_trade.user_id, fx_trade.purpose, fx_trade.requested_foreign_amount, fx_trade.borrow_amount, fx_tr … = $2::INTEGER ORDER BY fx_trade.created_at DESC, fx_trade.id DESC   LIMIT $3::INTEGER` |
| 288 | 50.86 | `UPDATE fx_pair SET gold_reserve=$1::NUMERIC(16, 6), foreign_reserve=$2::NUMERIC(16, 6), pool_version=$3::INTEGER, updated_at=$4::TIMESTAMP WITH TIME ZONE WHERE fx_pair.id = $5::INTEGER` |

profile pg_stat_activity：`[{'state': 'idle', 'type': 'Client', 'event': 'ClientRead', 'samples': 486}, {'state': 'idle in transaction', 'type': 'Client', 'event': 'ClientRead', 'samples': 255}, {'state': 'active', 'type': 'Client', 'event': 'ClientRead', 'samples': 13}, {'state': 'active', 'type': 'Lock', 'event': 'transactionid', 'samples': 9}, {'state': 'active', 'type': None, 'event': None, 'samples': 9}, {'state': 'idle in transaction', 'type': None, 'event': None, 'samples': 4}]`。

| calls | PGexec ms | query结构（无参数） |
|---:|---:|---|
| 221 | 3630.48 | `SELECT fx_pair.id  FROM fx_pair  WHERE fx_pair.id = $1::INTEGER FOR SHARE` |
| 296 | 1158.57 | `SELECT fx_pair.id, fx_pair.currency_code, fx_pair.currency_name, fx_pair.status, fx_pair.archived, fx_pair.gold_reserve, fx_pair.foreign_reserve, fx_pair.initial_price, fx_pair.buy_fee_ … eated_at, fx_pair.updated_at  FROM fx_pair  WHERE fx_pair.id = $1::INTEGER FOR UPDATE` |
| 448 | 266.48 | `SELECT (SELECT fx_market_data_state.history_ready  FROM fx_market_data_state  WHERE fx_market_data_state.pair_id = $1::INTEGER) AS anon_1, (SELECT coalesce(sum(fx_candle.gold_volume), $ … M fx_market_data_state  WHERE fx_market_data_state.pair_id = $21::INTEGER)) AS anon_5` |
| 254 | 154.76 | `INSERT INTO fx_trade (execution_id, execution_leg, pair_id, user_id, purpose, requested_foreign_amount, borrow_amount, max_gold_in, cover_all, side, input_amount, output_amount, min_out …  6), $20::VARCHAR, $21::VARCHAR, $22::TIMESTAMP WITH TIME ZONE) RETURNING fx_trade.id` |
| 254 | 121.28 | `INSERT INTO fx_pool_event (pair_id, created_at, kind, pre_gold_reserve, pre_foreign_reserve, post_gold_reserve, post_foreign_reserve, gold_volume, trade_id, audit_id) VALUES ($1::INTEGE … ERIC(16, 6), $8::NUMERIC(16, 6), $9::INTEGER, $10::BIGINT) RETURNING fx_pool_event.id` |
| 231 | 105.67 | `INSERT INTO fx_candle (pair_id, interval, bucket_start, open_price, high_price, low_price, close_price, gold_volume, n_trades, first_event_at, first_event_id, last_event_at, last_event_ … event_id END, n_events = (fx_candle.n_events + excluded.n_events), updated_at = now()` |
| 2006 | 90.87 | `SELECT "user".id AS user_id, "user".casdoor_id AS user_casdoor_id, "user".username AS user_username, "user".email AS user_email, "user".is_active AS user_is_active, "user".is_superuser  … uipped_title_id AS user_equipped_title_id  FROM "user"  WHERE "user".id = $1::INTEGER` |
| 324 | 69.27 | `INSERT INTO audit_event (ts, event_type, user_id, market_id, outcome_id, operator_user_id, ref_table, ref_id, payload, user_after, position_after, market_after) VALUES ($1::TIMESTAMP WI … HAR, $8::INTEGER, $9::JSON, $10::JSON, $11::JSON, $12::JSON) RETURNING audit_event.id` |
| 235 | 60.12 | `INSERT INTO fx_execution (user_id, pair_id, idempotency_key, request_payload, response_version, response_payload, created_at) VALUES ($1::INTEGER, $2::INTEGER, $3::VARCHAR, $4::JSON, $5 … :JSON, $5::INTEGER, $6::JSON, $7::TIMESTAMP WITH TIME ZONE) RETURNING fx_execution.id` |
| 227 | 55.60 | `SELECT fx_trade.id, fx_trade.execution_id, fx_trade.execution_leg, fx_trade.pair_id, fx_trade.user_id, fx_trade.purpose, fx_trade.requested_foreign_amount, fx_trade.borrow_amount, fx_tr … = $2::INTEGER ORDER BY fx_trade.created_at DESC, fx_trade.id DESC   LIMIT $3::INTEGER` |
| 254 | 46.27 | `UPDATE fx_pair SET gold_reserve=$1::NUMERIC(16, 6), foreign_reserve=$2::NUMERIC(16, 6), pool_version=$3::INTEGER, updated_at=$4::TIMESTAMP WITH TIME ZONE WHERE fx_pair.id = $5::INTEGER` |
| 234 | 44.14 | `UPDATE fx_execution SET response_payload=$1::JSON WHERE fx_execution.id = $2::INTEGER` |

| 轮次 | PG控制语句 | calls | PGexec ms |
|---|---|---:|---:|
| control | ROLLBACK | 11755 | 8.50 |
| control | BEGIN | 12563 | 8.20 |
| control | COMMIT | 805 | 0.87 |
| profile | ROLLBACK | 10136 | 7.38 |
| profile | BEGIN | 10841 | 7.35 |
| profile | COMMIT | 702 | 0.81 |

外部native样本有8/421条栈包含asyncpg dialect的_async_ping（inclusive，不能与叶帧占比相加）。大量BEGIN/ROLLBACK包含业务/会话及pre_ping等控制路径，不能全归于一种机制；cursor hook未覆盖它们，PG COMMIT exec计时也不能替代commit-driver与WAL sync测量。


## Fanout计数

ASGI send只计response.body消息数/bytes，不记录payload；可能包含空终止帧，不能与业务事件数直接相等。记录窗口跟随server stress观察（相对保护最多一个poll/调度周期尾差），不是完整客户端逐帧因果。

| 轮次 | route | body消息 | bytes |
|---|---|---:|---:|
| control | /history/fx/:id/:version/:interval/:segment.json | 144 | 9486 |
| control | GET /api/v1/fx/pairs/:id/chart | 454 | 80152 |
| control | GET /api/v1/fx/pairs/:id/my-trades | 524 | 705642 |
| control | GET /api/v1/fx/pairs/:id/snapshot | 524 | 116852 |
| control | GET /api/v1/fx/stream/:id | 24900 | 15348300 |
| control | GET /api/v1/stream/trading | 28185 | 14607857 |
| control | GET /api/v1/user/trading-context | 574 | 445489 |
| control | GET /api/v1/user/trading-prices | 138 | 34638 |
| control | GET /api/v1/user/trading-versions | 392 | 72590 |
| control | POST /api/v1/fx/pairs/:id/net/quote | 744 | 303898 |
| control | POST /api/v1/fx/pairs/:id/net/trades | 530 | 238693 |
| control | POST /api/v1/fx/pairs/:id/short/cover | 40 | 6280 |
| control | POST /api/v1/fx/pairs/:id/short/quote | 42 | 11673 |
| profile | GET /api/v1/fx/pairs/:id/my-trades | 452 | 581690 |
| profile | GET /api/v1/fx/pairs/:id/snapshot | 452 | 100796 |
| profile | GET /api/v1/fx/stream/:id | 22000 | 13549500 |
| profile | GET /api/v1/stream/trading | 24353 | 12606984 |
| profile | GET /api/v1/user/trading-context | 518 | 402039 |
| profile | GET /api/v1/user/trading-prices | 142 | 35642 |
| profile | GET /api/v1/user/trading-versions | 396 | 73327 |
| profile | POST /api/v1/fx/pairs/:id/net/quote | 672 | 274766 |
| profile | POST /api/v1/fx/pairs/:id/net/trades | 474 | 212124 |
| profile | POST /api/v1/fx/pairs/:id/short/cover | 32 | 5024 |
| profile | POST /api/v1/fx/pairs/:id/short/quote | 36 | 9998 |

## 证据边界与运行记录

具体高占用函数、外部叶帧及调用栈保留在analysis.json与profile/cpu.speedscope.json、profile/native-stress.speedscope.json。队列增长/持有数/池占用完整曲线在每轮resources.jsonl，严格stress窗口见stress-started.json、protection.json、instrument-windows.json；pg-activity与snapshot保留queryid、阻塞者和结构化SQL，无参数/token。代码中的FX公共流每帧create/cancel/gather任务、私人流is_disconnected与PricingPublisher全订阅者offer/model_dump只是解释候选，不能把所有async/JSON样本自动归给其中某处；需要以具体栈支持。

本轮未切换专门stress模式、未新增优化实验、未运行全量或浏览器测试。监测错误：{'control': ([], []), 'profile': ([], [])}。命令在commands.json，harness是实际临时脚本快照，依赖/tmp旧辅助与保留融资diff的源码；固定目录/库名已存在，不能开箱重跑，需换新路径/库名。新测试库保留，不drop旧库。清理状态见cleanup.json（服务、PG、tokens）。
