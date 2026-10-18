# FX 唯一 writer 最终 A/B 结果

本轮确认了普通账户读取与市场执行排队的解耦。stress 下，versions P99 从 **5653ms 降到 284ms**，成交后 context P99 从 **5690ms 降到 341ms**。写入容量仍受限：两边均触发“30 秒窗口成功交易 P99 >2000ms”的既定保护，**burst 均未执行**。after 的 stress 实际成功吞吐为 10.909/s，before 为 10.110/s；这不表示通过了名义 25 cycles/s 的容量目标。

after 的成交延迟也有回退：readheavy 成交 P50 58→85ms，normal 62→85ms，active 124→151ms；stress 成交 P50 4555→8399ms、P99 9816→10021ms。迁移带来的主要收益是读取在写入积压时保持可用，不能描述成整体交易延迟或容量全面改善。

## 实验来源与范围

- before：`330f4e683ec5564dea6ea59983351c3c8e128d9d`；after：`6c7675722cda265bd205dced6ce0f939ae7ac769`，均为 committed backend 的 git archive。
- 单机顺序运行一次 before→after；全新隔离库 `fx_single_writer_before_test` / `fx_single_writer_after_test`，相同初始化 helper、seed 20261017、100 用户、200 SSE（public/private 各100）、cash/finance/short 60/20/20、pool 10+20、单 Uvicorn。没有生产数据或产品代码修改。
- HTTP/readheavy/resource sampler 与上一轮字节相同：readheavy 60s、1200 固定读+30定时cycle；readonly 30s；normal 60s；active 45s；stress 30s；原 burst 观察10s。其余角色准备、历史请求、版本探测和停止保护均沿用。每个cycle包含真实server quote，采用完成后重新安排的原时序，不积累追赶任务；过载时实际发起率会下降。
- 两个实验源码的 SSE `MAX_PER_IP=200`（每topic/IP），支持同一loopback IP的此负载；主工作树另有生产限制10，整合保留10。本结果不说明生产同IP用户容量，也不表示生产IP策略已修改。
- PostgreSQL14 `fsync=on`、`synchronous_commit=on`、I/O/WAL计时开启、`pg_stat_statements`启用；没有profiler或额外负载。

## 各阶段实际结果

成功交易/s = 实际成功成交数 / driver 实际阶段时长。时长包含原有阶段末尾观察与在途请求排空；不是单取配置的30/45/60秒。HTTP统计排除cycle计数和SSE流；quote_blocked表示报价业务不允许继续成交，并非HTTP错误或成功交易。所有下表已完成HTTP请求均为ok，429/5xx/network/timeout合计0；不能据此忽略延迟保护。

|阶段|版本|实际秒|发起cycles|成功成交|成功交易/s|完成HTTP|HTTP错误|quote_blocked|结果|
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
|readheavy|before|60.010|30|30|0.500|1350|0|0|完成|
|readheavy|after|60.064|30|30|0.499|1350|0|0|完成|
|readonly|before|31.005|0|0|0.000|200|0|0|完成|
|readonly|after|31.004|0|0|0.000|200|0|0|完成|
|normal|before|61.004|157|156|2.557|1180|0|1|完成|
|normal|after|61.296|156|155|2.529|1176|0|1|完成|
|active|before|46.120|336|324|7.025|1933|0|12|完成|
|active|after|46.276|335|327|7.066|1946|0|8|完成|
|stress|before|33.730|354|341|10.110|1907|0|13|延迟保护停止|
|stress|after|35.015|395|382|10.909|2124|0|13|延迟保护停止|

两边readheavy均完成300×4=1200固定读取和30成功成交，合计1350个完成HTTP请求。normal/active实际成功吞吐基本持平；stress在两个保护截断和不同排空时长下观察到约7.9%的成功吞吐差异。只做了一次顺序实验，不作稳定容量或统计显著性结论。

|阶段|版本|成功成交P50 ms|P95 ms|P99 ms|
|---|---|---:|---:|---:|
|readheavy|before|58.353|101.090|105.641|
|readheavy|after|84.556|108.895|114.477|
|normal|before|61.650|188.452|293.099|
|normal|after|85.152|165.472|258.859|
|active|before|123.852|570.673|759.373|
|active|after|151.403|569.193|659.105|
|stress|before|4555.022|9142.304|9816.311|
|stress|after|8398.664|9947.800|10021.442|

## 读取延迟

均为成功响应分布；n为before→after样本数。行情snapshot与账户context分开列出，小样本尾部和回退也保留。

|阶段|读取|n before→after|P95 ms before→after|P99 ms before→after|
|---|---|---|---|---|
|readheavy|固定context|300→300|56.710→56.623|83.212→93.441|
|readheavy|固定versions|300→300|36.472→28.903|93.524→60.987|
|readheavy|固定prices|300→300|29.330→23.736|59.756→49.211|
|readheavy|固定行情snapshot|300→300|21.108→21.788|25.577→44.226|
|readonly|周期versions|200→200|36.687→32.204|43.914→80.199|
|normal|周期versions|399→398|139.402→91.382|242.099→238.854|
|normal|成交后context|156→155|214.758→128.575|442.576→146.118|
|normal|成交后行情snapshot|156→155|91.471→55.447|207.022→60.375|
|normal|prices repair|0→2|—→74.893|—→74.893|
|active|周期versions|300→300|471.027→146.650|696.354→266.098|
|active|成交后context|324→327|540.522→171.745|699.614→286.629|
|active|成交后行情snapshot|324→327|93.268→68.793|193.781→95.074|
|active|prices repair|1→3|54.546→130.356|54.546→130.356|
|stress|周期versions|165→198|5408.278→182.185|5653.042→284.407|
|stress|成交后context|341→382|5514.606→186.845|5690.324→341.270|
|stress|成交后行情snapshot|341→382|96.729→67.278|137.939→170.535|
|stress|prices repair|24→3|4483.592→76.719|5569.192→76.719|

读取改善集中在active/stress的versions/context。回退包括readheavy固定context P99 83→93ms、固定snapshot 26→44ms，readonly versions P99 44→80ms，以及stress成交后snapshot P99 138→171ms；不能声称所有读取分位数都改善。公共精确历史协议仍保留FOR SHARE，可能等待数据库；本实验不证明历史组合SSE完全免数据库等待。

## Writer队列与执行

before没有FX actor，记录为absent/unavailable；after已附加observer。下面只列after实测，不以before=0比较。queue wait是成功接受enqueue至dequeue返回；execution是dequeue返回至下一次get调用或consumer退出，包含handler、commit/发布/通知、future处理及恢复，不含空闲queue.get。各列分位数来自不同样本排序，**不能把queue P99与execution P99相加当作HTTP P99**。

|阶段|命令|n|排队均值 ms|排队P95 ms|排队P99 ms|执行均值 ms|执行P95 ms|执行P99 ms|
|---|---|---:|---:|---:|---:|---:|---:|---:|
|readheavy|NetTrade|29|0.103|0.110|0.559|40.943|58.497|59.776|
|normal|NetTrade|144|3.146|24.118|35.704|48.256|93.081|183.411|
|normal|ShortCover|11|0.066|0.085|0.085|43.377|55.672|55.672|
|active|NetTrade|297|78.920|366.002|494.246|76.423|140.642|269.690|
|active|ShortCover|30|65.041|197.546|335.886|71.569|118.808|129.090|
|stress|NetTrade|349|7125.835|9794.032|9862.830|87.780|148.165|243.033|
|stress|ShortCover|33|7453.438|9561.493|9666.260|93.223|133.453|270.659|

stress净交易排队P99约9863ms，执行P99约243ms；与CPU接近单核占满同时出现，支持“普通读已解耦、写队列仍积压”的判断。没有profiler，不能进一步断言具体CPU函数或把全部积压归因于某个SQL/锁。phase通过原50ms轮询更新，紧邻阶段边界的少量SQL/actor样本可能归到相邻phase，例如readheavy actor为29条而HTTP成功30条；不代表丢失成交。

## SQL、资源与锁观测

SQL cursor数量按整个服务器phase累计，包含阶段末尾排空；资源/PG采样窗口在stress截于保护时刻。两者时间范围不同，不能直接混除计算每请求数据库成本。CPU为进程样本均值，100%约一个核心；PG CPU为backend采样差量近似，PG执行ms为pg_stat_statements阶段快照差量。

|阶段|版本|SQL cursor总数|App CPU %|pool峰值|eventloop P99 ms|PG CPU近似s|PG执行ms|
|---|---|---:|---:|---:|---:|---:|---:|
|readheavy|before|7580|36.35|8|11.159|1.930|415.371|
|readheavy|after|7449|36.32|7|15.118|1.900|403.654|
|readonly|before|1453|15.69|5|3.161|0.390|64.303|
|readonly|after|1447|15.26|3|2.867|0.340|58.735|
|normal|before|10750|43.52|10|28.690|2.950|1009.876|
|normal|after|10252|42.10|9|26.641|2.710|904.686|
|active|before|18510|79.98|15|30.115|5.740|3813.582|
|active|after|17809|81.36|14|31.422|5.610|3404.566|
|stress|before|18134|96.76|16|122.961|5.910|5181.797|
|stress|after|19160|97.88|18|149.020|5.060|3960.662|

|阶段|HTTP business SQL before→after|after writer后台SQL|live-read SQL before→after|
|---|---:|---:|---:|
|readheavy|854→216|503|6327→6331|
|readonly|0→0|0|1424→1418|
|normal|4289→1078|2721|4541→4534|
|active|8948→2412|5773|5698→5735|
|stress|9293→2866|6767|5037→5534|

writer SQL现记为background:fx_writer，不能把HTTP business SQL减少全部当作数据库工作消失。总SQL在normal/active略降，stress随实际成功成交数增加而上升。目录、my-trades join、业务报价/写入、pool_version/history SQL继续保留；此次迁移没有把所有读取变为零SQL。

以下为User FOR UPDATE的**cursor查询总耗时**，不是纯锁等待。before按两个HTTP路由分开，after actor行包含所有命令，样本组合不同，不做同分布锁性能比较。

|阶段|版本/归属|n|均值ms|P95 ms|P99 ms|最大ms|
|---|---|---:|---:|---:|---:|---:|
|readheavy|before/net/trades|34|0.580|1.226|1.401|1.874|
|readheavy|after/actor所有命令|29|0.604|1.263|1.577|2.017|
|normal|before/net/trades|172|0.970|2.828|7.385|12.343|
|normal|before/cover|12|0.382|0.553|0.553|0.853|
|normal|after/actor所有命令|155|0.670|1.520|4.710|6.551|
|active|before/net/trades|353|1.957|5.529|12.286|16.344|
|active|before/cover|31|2.105|5.599|6.338|12.348|
|active|after/actor所有命令|327|2.408|6.854|13.467|16.747|
|stress|before/net/trades|373|3.464|11.231|16.997|21.600|
|stress|before/cover|26|2.684|4.557|5.103|10.360|
|stress|after/actor所有命令|382|3.723|9.363|15.051|167.318|

|阶段|版本|观察到Lock backend样本|其中User FOR UPDATE样本|blocking PID非空样本|
|---|---|---:|---:|---:|
|readheavy|before|0|0|0|
|readheavy|after|0|0|0|
|readonly|before|0|0|0|
|readonly|after|0|0|0|
|normal|before|0|0|0|
|normal|after|0|0|0|
|active|before|4|0|4|
|active|after|1|0|1|
|stress|before|10|0|11|
|stress|after|6|0|6|

PG wait为0.5秒离散采样，0样本只表示未采到；不是证明没有User锁等待。精确逐查询锁等待时长没有测得。before stress GATE waiting峰值133、net交易等待P99约5451ms、context等待P99约5501ms；after GATE已删除，字段为null/absent，不虚构零计时。

## SSE与可见性边界

global SSE指标含smoke/connect/角色准备及各阶段，event age依赖客户端/服务端时钟；下面不是单独stress分布。

|类型|版本|首帧n|首帧P99 ms|收到frames|payload bytes|event age P95/P99 ms|接收间隔P99 ms|
|---|---|---:|---:|---:|---:|---|---:|
|public|before|100|67.246|85108|49925486|32.313/42.600|2079.676|
|public|after|100|62.625|85008|49870887|34.494/42.669|2051.902|
|private|before|100|51.611|91828|45019991|50.206/169.961|2064.047|
|private|after|100|46.421|91620|44918553|42.677/184.530|2057.289|

两边直到active阶段末均保持public/private各100连接。stress保护使driver关闭SSE，阶段末connected=0；保护原因是成交P99，不能把这个0当成先发生的服务端断流。readonly公共SSE无交易帧符合该阶段无交易；私人仍有心跳。全量分阶段帧/字节留在summary/analysis中。

继承的成交到首个observer可见延迟未产生有效匹配：normal/active/stress的visibility count均为0，unmatched分别为before 156/324/341、after 155/327/382。因此不能量化逐笔成交到客户端可见延迟，也不能用其空值声称零延迟。保留该限制，没有扩大或修改测量平台。

## 保护与统计时间边界

|版本|stress起点 上海时间|保护前秒数|保护后至阶段结束秒数|整阶段秒数|采样资源窗口秒数|未执行|
|---|---|---:|---:|---:|---:|---|
|before|2026-10-18T15:40:40.673+08:00|30.713|3.018|33.730|30.360|burst；orders配置0|
|after|2026-10-18T15:45:49.909+08:00|30.646|4.370|35.015|30.125|burst；orders配置0|

两边保护原因相同：`30s window: successful trade P99 exceeds 2000ms`。driver退出码各1是保护停止；外层run.py已按finally清理并完成两边，退出码0不等于性能容量验收通过。HTTP/SQL/actor分布通常含排空，保护窗口是滚动30秒；不要把整阶段P99当成触发瞬间的同一统计样本。

## 验证、清理与原始证据

已执行一次指定run.py --start-authorized，再执行analyze.py；归档/哈希、check_observer.py --commit最终SHA、py_compile均通过。两边pg-monitor-errors均[]，backend日志均未出现Traceback、QueuePool limit或CancelledError。未运行浏览器或交互流程测试，未新增产品测试或生产改动。

清理核验：本轮run/driver/monitor/count_server进程为[]，18543无监听，before/after的private/tokens.json均不存在；主agent独立核实后已停止隔离PG。本agent没有停止PG。

未测项目：burst因保护均未执行；orders原配置0；无重复试验/统计置信区间；新库历史较浅；无跨市场性能、跨进程部署、外部网络延迟结论；未测精确逐查询lock wait与逐笔SSE visibility。公共精确历史仍可等DB。资金正确性和主工作树整合验证由主agent另外报告，不把本压测当作资金验收。

复现命令：

```bash
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python /tmp/sunyunbo/fx-single-writer-ab-20261018/run.py --start-authorized
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python /tmp/sunyunbo/fx-single-writer-ab-20261018/analyze.py
```

已有结果目录/数据库不得覆盖；本轮不重跑。实际完整driver参数见各variant `actual-command.json`。轻量observer只增加时钟读数、字典/counter/sample操作及actor context标签，无额外SQL/task/await；overhead未独立benchmark或扣除，after承担其适用的actor计时成本，共有观测两边一致。

原始证据：[analysis.json](analysis.json)、[before HTTP summary](before/http/summary.json)、[after HTTP summary](after/http/summary.json)、[before protection](before/protection.json)、[after protection](after/protection.json)、[before源码hash](before-source.json)、[after源码hash](after-source.json)、[harness manifest](harness-manifest.json)、[harness镜像](harness/)、[准备记录](harness/PREPARED.md)。每边同时保留requests.jsonl、phases.jsonl、PG活动/SQL快照、SQL分类、body计数、资源/eventloop、query elapsed、writer timings/counters、完整日志；不要只看本报告舍入值。
