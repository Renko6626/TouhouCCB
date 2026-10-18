# read_dependencies：3 SELECT 与 UNION ALL 本机对照

基线 `/tmp/sunyunbo/thccb-fx-read-checkpoint-20261017`，实验时HEAD `e3b2087f4828f76a79cfe152be768d3621829801`。两版都包含尚未提交的融资优化4文件（net_trading、loan_service与对应测试）；本次query测试文件两版相同，只有before的trading_context SQLAlchemy import和read_dependencies恢复3 SELECT，after保留UNION ALL。全backend差异见scope.diff，源码完整清单见source-hashes.json。实验脚本未修改生产源或部署，未进行内存迁移。实验后主agent将query生产代码与对应原测试提交为 `71b8a83`；本次A/B的基线和源码hash仍指实验快照，融资4文件仍为实验时的未提交改动。

本机PG14隔离cluster，Unix socket，port55489，不监听TCP；每版新建 `fx_dependency_query_{before,after}_test`。单Uvicorn worker，127.0.0.1:18543，池10+20。客户端、服务端、数据库同机，结果只用于局部对照。按before→after顺序各一轮，未随机交叉重复，因此小幅延迟/吞吐差异可能含缓存、调度与同机噪声。

## 固定完成数HTTP读对照

混合场景前执行，同100用户、每端点100次预热后1000次测量、并发10，身份及参数顺序相同。prices显式传入当前pair key，允许检查这一路径是否受依赖查询影响。这不是完整交易容量。SQL计数来自SQLAlchemy before_cursor_execute，按阶段+路由归属（不含参数）；后台SQL不计入每请求，不包含asyncpg pool_pre_ping等driver内部控制SQL。

| 版本 | 接口/阶段 | 成功/请求 | SQL/请求 | 依赖SQL分类/次数 | 均值 ms | P99 ms | 请求/s | app CPU ms/请求（近似） |
|---|---|---:|---:|---|---:|---:|---:|---:|
| before | versions/预热 | 100/100 | 7.14 | {'dependency_market': 100, 'dependency_wallet': 100, 'dependency_short': 100} | 70.83 | 188.91 | 137.70 | 4.01 |
| before | versions/测量 | 1000/1000 | 7.00 | {'dependency_market': 1000, 'dependency_wallet': 1000, 'dependency_short': 1000} | 62.88 | 100.47 | 158.55 | 6.37 |
| before | prices/预热 | 100/100 | 5.00 | {'dependency_market': 100, 'dependency_wallet': 100, 'dependency_short': 100} | 58.37 | 146.77 | 168.01 | 3.93 |
| before | prices/测量 | 1000/1000 | 5.00 | {'dependency_market': 1000, 'dependency_wallet': 1000, 'dependency_short': 1000} | 53.59 | 81.12 | 185.89 | 5.54 |
| after | versions/预热 | 100/100 | 5.08 | {'dependency_union': 100} | 63.81 | 176.76 | 152.88 | 6.34 |
| after | versions/测量 | 1000/1000 | 5.00 | {'dependency_union': 1000} | 59.80 | 98.32 | 166.54 | 5.94 |
| after | prices/预热 | 100/100 | 3.00 | {'dependency_union': 100} | 63.20 | 139.85 | 154.67 | 8.35 |
| after | prices/测量 | 1000/1000 | 3.00 | {'dependency_union': 1000} | 52.23 | 78.57 | 190.62 | 5.05 |

## 原混合场景

100用户，公开/私人SSE各100，角色现金/融资/空头=60/20/20，seed20261017；初始现金100000、源池1000000/200000、FX费率0。沿用原HTTP脚本、轻量SQL计数、0.5秒队列采样、1秒资源采样与门闩挂钩，无CPU profiler。阶段：冒烟10s、入场间隔0.3s、只读30s、正常60s、活跃45s、高压30s、集中下单后观察10s，条件单跳过。stress目标为所有用户合计25交易循环/s。未改变30秒滚动错误率>5%/成交P99>2s保护；停止后的阶段不补跑、不放宽阈值。

| 版本 | 阶段 | 成交/循环 | 成交/s | HTTP/s | 成交P99 ms | app CPU均值 % | SQL总数 | 依赖SQL数 | HTTP结果 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| before | readonly | 0/0 | 0.0 | 6.451 | — | 18.4 | 2498 | 1224 | {'ok': 200} |
| before | normal | 150/150 | 2.459 | 18.901 | 212.687 | 45.0 | 13437 | 2966 | {'ok': 1153} |
| before | active | 337/339 | 7.301 | 44.022 | 583.834 | 82.5 | 22846 | 3126 | {'ok': 2032} |
| before | stress | 361/412 | 10.59 | 64.331 | 14178.185 | 97.9 | 23007 | 2439 | {'ok': 2154, 'timeout': 21, 'version_conflict': 18} |
| after | readonly | 0/0 | 0.0 | 6.451 | — | 17.1 | 1682 | 408 | {'ok': 200} |
| after | normal | 151/152 | 2.475 | 19.065 | 278.394 | 42.3 | 11573 | 1048 | {'ok': 1163} |
| after | active | 337/345 | 7.279 | 44.168 | 967.788 | 81.2 | 20953 | 1160 | {'ok': 2045} |
| after | stress | 327/388 | 9.561 | 60.201 | 13366.67 | 96.7 | 20046 | 885 | {'ok': 2012, 'timeout': 28, 'version_conflict': 19} |

| 版本 | 阶段 | 门闩获取数 | 持有均值 ms | 等待P99 ms | 等待队列峰值 | 池占用采样峰值 | SSE公开/私人 |
|---|---|---:|---:|---:|---:|---:|---|
| before | readonly | 0 | — | — | 0 | 2 | 100/100 |
| before | normal | 167 | 33.11 | 46.36 | 0 | 7 | 100/100 |
| before | active | 374 | 54.34 | 341.14 | 3 | 10 | 100/100 |
| before | stress | 438 | 69.48 | 8512.88 | 100 | 10 | 0/0 |
| after | readonly | 0 | — | — | 0 | 1 | 100/100 |
| after | normal | 168 | 29.22 | 71.18 | 1 | 7 | 100/100 |
| after | active | 368 | 57.57 | 579.06 | 6 | 10 | 100/100 |
| after | stress | 414 | 74.76 | 9123.56 | 100 | 21 | 0/0 |

before：停止原因 `30s window: successful trade P99 exceeds 2000ms`；保护停止时阶段 stress，短于计划时长阶段 []，未运行阶段 ['burst']；全轮池峰值 22/30，等待峰值 100；池耗尽日志 0，Traceback 0，CancelledError 0。

after：停止原因 `30s window: successful trade P99 exceeds 2000ms`；保护停止时阶段 stress，短于计划时长阶段 []，未运行阶段 ['burst']；全轮池峰值 21/30，等待峰值 100；池耗尽日志 0，Traceback 0，CancelledError 0。

| 版本 | 阶段 | 版本冲突 | 报价不可执行（未发成交） | timeout |
|---|---|---:|---:|---:|
| before | readonly | 0 | 0 | 0 |
| before | normal | 0 | 0 | 0 |
| before | active | 0 | 2 | 0 |
| before | stress | 18 | 12 | 21 |
| after | readonly | 0 | 0 | 0 |
| after | normal | 0 | 1 | 0 |
| after | active | 0 | 8 | 0 |
| after | stress | 19 | 14 | 28 |

两版保护停止均结束公开/私人各100条SSE（阶段尾值0/0），这是驱动停止后的结果；原驱动还会取消阶段reader与收尾任务，但没有单独记录HTTP/task取消计数，因此取消数记为未测量，不能把它当0。timeout不能证明服务端未提交，没有自动重试写请求。


## 解读与验证

固定测量的总SQL/请求：versions 7→5、prices 5→3；依赖身份SQL均由3→1，这是本轮明确验证的减少。versions均值延迟62.88→59.80ms，prices53.59→52.23ms；应用CPU约6.37→5.94ms/请求、5.54→5.05ms/请求。仅单轮且CPU为采样近似，不能据此承诺稳定百分比收益。混合normal/active成交吞吐基本相当，after的成交P99反而更高；两版stress均因P99保护停止，未运行burst，本轮没有证据证明完整交易容量提高。

UNION ALL将一次依赖身份读取的客户端SQL执行由3次合为1次；三个分支仍需读取各自表，不能解释为表扫描减少2/3。混合SQL总量受完成请求数、SSE事件、阶段边界影响，SQL下降不能直接推导成交吞吐提升。固定读单独报告；饱和阶段的停测、冲突和排队会改变可比较工作量。门闩按每次获取计（可能重试），跨阶段请求按各hook当时的阶段归属，不能以成交成功数直接除总SQL推导固定单笔SQL。CPU仅应用进程，100%约为一个核，不包含PG/驱动。固定读CPU秒按1秒资源样本与0.5秒phase采样窗口重叠积分（默认窗口内CPU均匀），再除固定请求数；包含少量阶段边界/后台开销，仅近似，不是精确请求CPU归因。

PG实际测试（工作目录 `/tmp/sunyunbo/thccb-fx-read-checkpoint-20261017/backend`）：`TEST_PG_DATABASE_URL='postgresql+asyncpg://sunyunbo@/fx_dependency_context_test?host=/tmp/sunyunbo/fx-pool-incident-20261017/socket&port=55489' /data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest -q -m pg tests/pg/test_pg_trading_context.py`，5 passed in 2.87s，非skip。独立测试在before seed准备阶段完成，未与固定测量/混合负载重叠。未运行全量或浏览器交互测试。

执行记录：harness/是本次实际脚本快照（修订说明见harness/REVISIONS.md），依赖旧/tmp harness及包含未提交融资diff的基线；固定副本目录与测试库现已存在。再次执行须保留相同融资diff，换新目录/库名并调整路径，不能直接开箱重跑；reproduce.sh仅记录本次编排。启动同设置隔离PG后，实际入口为`backend/venv/bin/python /tmp/sunyunbo/fx-dependency-query-ab-20261018/run.py`；prepare.py从指定dirty worktree构造副本，adapt.py修改hooks，read_driver.py固定读，build_report.py聚合。结果harness/保存本次脚本，不含backend副本或token。每版raw：http/summary.json、http/requests.jsonl、resources.jsonl、sql-counts.json、dependency-sql.json、gates.json、gate-queue.jsonl、backend.log。新建库保留；服务和本次启动的PG已关闭，令牌已删除。
