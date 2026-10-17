import json,collections,hashlib,shutil,subprocess
from pathlib import Path
root=Path('/data/sunyunbo/www/TouhouCCB/output/fx-dependency-query-ab-20261018');harness=Path(__file__).resolve().parent
# Retain the original mixed-scenario aggregation, then replace its report.
exec((harness/'summarize.py').read_text().split("lines=['#")[0])
for variant,info in result.items():
    p=root/variant
    info['deterministic']=json.loads((p/'deterministic.json').read_text())
    resources=[json.loads(l) for l in (p/'resources.jsonl').read_text().splitlines()];queue=[json.loads(l) for l in (p/'gate-queue.jsonl').read_text().splitlines()]
    sql=json.loads((p/'sql-counts.json').read_text());deps=json.loads((p/'dependency-sql.json').read_text())
    info['dependency_sql']=deps
    for phase,row in info['deterministic'].items():
        route='GET /api/v1/user/trading-'+('versions' if '_versions_' in phase else 'prices')
        row['sql_executions']=sum(x['executions'] for x in sql if x['phase']==phase and x['route']==route)
        row['sql_per_request']=row['sql_executions']/row['requests']
        samples=[x for x in queue if x['phase']==phase]
        if samples:
            lo,hi=samples[0]['t'],samples[-1]['t']+.5
            cpu=sum(x['app_cpu_percent']/100*max(0,min(x['t'],hi)-max(prev['t'],lo)) for prev,x in zip(resources,resources[1:]))
            row['app_cpu_seconds_approx']=cpu;row['app_cpu_seconds_per_request_approx']=cpu/row['requests']
        else:row['app_cpu_seconds_approx']=None;row['app_cpu_seconds_per_request_approx']=None
        row['dependency_executions']={x['kind']:x['executions'] for x in deps if x['phase']==phase and x['route']==route}
    requested=['readonly','normal','active','stress','burst'];planned={'readonly':30,'normal':60,'active':45,'stress':30,'burst':10}
    info['missing_stages']=[name for name in requested if name not in [s['name'] for s in info['stages']]]
    info['protection_stopped_phase']=info['stages'][-1]['name'] if info['abort'] and info['stages'] else None
    info['incomplete_stages']=[s['name'] for s in info['stages'] if s['duration_s']+0.5<planned[s['name']]]
    for stage in info['stages']:
        rawstage=next(s for s in json.loads((p/'http/summary.json').read_text())['stages'] if s['name']==stage['name'])
        stage['cycle_results']=next((r['results'] for r in json.loads((p/'http/summary.json').read_text())['requests'] if r['phase']==stage['name'] and r['name']=='cycle'),{})
        stage['http_requests_per_s']=rawstage['completed_requests_per_s'];stage['http_requests']=rawstage['completed_requests']
        stage['sql_executions']=sum(x['executions'] for x in sql if x['phase']==stage['name'])
        stage['dependency_executions']=sum(x['executions'] for x in deps if x['phase']==stage['name'])
(root/'comparison.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
lines=['# read_dependencies：3 SELECT 与 UNION ALL 本机对照','','基线 `/tmp/sunyunbo/thccb-fx-read-checkpoint-20261017`，实验时HEAD `e3b2087f4828f76a79cfe152be768d3621829801`。两版都包含尚未提交的融资优化4文件（net_trading、loan_service与对应测试）；本次query测试文件两版相同，只有before的trading_context SQLAlchemy import和read_dependencies恢复3 SELECT，after保留UNION ALL。全backend差异见scope.diff，源码完整清单见source-hashes.json。实验脚本未修改生产源或部署，未进行内存迁移。实验后主agent将query生产代码与对应原测试提交为 `71b8a83`；本次A/B的基线和源码hash仍指实验快照，融资4文件仍为实验时的未提交改动。','','本机PG14隔离cluster，Unix socket，port55489，不监听TCP；每版新建 `fx_dependency_query_{before,after}_test`。单Uvicorn worker，127.0.0.1:18543，池10+20。客户端、服务端、数据库同机，结果只用于局部对照。按before→after顺序各一轮，未随机交叉重复，因此小幅延迟/吞吐差异可能含缓存、调度与同机噪声。','','## 固定完成数HTTP读对照','','混合场景前执行，同100用户、每端点100次预热后1000次测量、并发10，身份及参数顺序相同。prices显式传入当前pair key，允许检查这一路径是否受依赖查询影响。这不是完整交易容量。SQL计数来自SQLAlchemy before_cursor_execute，按阶段+路由归属（不含参数）；后台SQL不计入每请求，不包含asyncpg pool_pre_ping等driver内部控制SQL。','','| 版本 | 接口/阶段 | 成功/请求 | SQL/请求 | 依赖SQL分类/次数 | 均值 ms | P99 ms | 请求/s | app CPU ms/请求（近似） |','|---|---|---:|---:|---|---:|---:|---:|---:|']
for v,i in result.items():
    for phase,r in i['deterministic'].items():
        endpoint='versions' if '_versions_' in phase else 'prices'
        lines.append(f"| {v} | {endpoint}/{'预热' if phase.endswith('_warmup') else '测量'} | {r['statuses'].get('200',0)}/{r['requests']} | {r['sql_per_request']:.2f} | {r['dependency_executions']} | {r['latency_ms']['mean']:.2f} | {r['latency_ms']['p99']:.2f} | {r['requests_per_s']:.2f} | {1000*r['app_cpu_seconds_per_request_approx']:.2f} |")
lines+=['','## 原混合场景','','100用户，公开/私人SSE各100，角色现金/融资/空头=60/20/20，seed20261017；初始现金100000、源池1000000/200000、FX费率0。沿用原HTTP脚本、轻量SQL计数、0.5秒队列采样、1秒资源采样与门闩挂钩，无CPU profiler。阶段：冒烟10s、入场间隔0.3s、只读30s、正常60s、活跃45s、高压30s、集中下单后观察10s，条件单跳过。stress目标为所有用户合计25交易循环/s。未改变30秒滚动错误率>5%/成交P99>2s保护；停止后的阶段不补跑、不放宽阈值。','','| 版本 | 阶段 | 成交/循环 | 成交/s | HTTP/s | 成交P99 ms | app CPU均值 % | SQL总数 | 依赖SQL数 | HTTP结果 |','|---|---|---:|---:|---:|---:|---:|---:|---:|---|']
for v,i in result.items():
    for s in i['stages']:
        cpu=f"{s['cpu_mean']:.1f}" if s['cpu_mean'] is not None else '—'
        lines.append(f"| {v} | {s['name']} | {s['successes']}/{s['cycles']} | {s['successes_per_s']} | {s['http_requests_per_s']} | {s['trade_latency'].get('p99_ms','—')} | {cpu} | {s['sql_executions']} | {s['dependency_executions']} | {s['http_results']} |")
lines+=['','| 版本 | 阶段 | 门闩获取数 | 持有均值 ms | 等待P99 ms | 等待队列峰值 | 池占用采样峰值 | SSE公开/私人 |','|---|---|---:|---:|---:|---:|---:|---|']
for v,i in result.items():
    for s in i['stages']:
        h,w=s['gate_held'],s['gate_wait']
        lines.append(f"| {v} | {s['name']} | {h['count'] if h else 0} | {format(h['mean_ms'],'.2f') if h else '—'} | {format(w['p99_ms'],'.2f') if w else '—'} | {s['queue_peak']} | {s['pool_peak_sampled']} | {s['sse_public']}/{s['sse_private']} |")
for v,i in result.items():
    lines += ['',f"{v}：停止原因 `{i['abort']}`；保护停止时阶段 {i['protection_stopped_phase']}，短于计划时长阶段 {i['incomplete_stages']}，未运行阶段 {i['missing_stages']}；全轮池峰值 {i['pool_peak']}/30，等待峰值 {i['queue_peak']}；池耗尽日志 {i['pool_errors']}，Traceback {i['tracebacks']}，CancelledError {i['cancelled_mentions']}。"]
lines+=['','| 版本 | 阶段 | 版本冲突 | 报价不可执行（未发成交） | timeout |','|---|---|---:|---:|---:|']
for v,i in result.items():
    for s in i['stages']:
        lines.append(f"| {v} | {s['name']} | {s['trade_results'].get('version_conflict',0)} | {s['cycle_results'].get('quote_blocked',0)} | {s['http_results'].get('timeout',0)} |")
lines+=['','两版保护停止均结束公开/私人各100条SSE（阶段尾值0/0），这是驱动停止后的结果；原驱动还会取消阶段reader与收尾任务，但没有单独记录HTTP/task取消计数，因此取消数记为未测量，不能把它当0。timeout不能证明服务端未提交，没有自动重试写请求。','']
lines+=['','## 解读与验证','','固定测量的总SQL/请求：versions 7→5、prices 5→3；依赖身份SQL均由3→1，这是本轮明确验证的减少。versions均值延迟62.88→59.80ms，prices53.59→52.23ms；应用CPU约6.37→5.94ms/请求、5.54→5.05ms/请求。仅单轮且CPU为采样近似，不能据此承诺稳定百分比收益。混合normal/active成交吞吐基本相当，after的成交P99反而更高；两版stress均因P99保护停止，未运行burst，本轮没有证据证明完整交易容量提高。','','UNION ALL将一次依赖身份读取的客户端SQL执行由3次合为1次；三个分支仍需读取各自表，不能解释为表扫描减少2/3。混合SQL总量受完成请求数、SSE事件、阶段边界影响，SQL下降不能直接推导成交吞吐提升。固定读单独报告；饱和阶段的停测、冲突和排队会改变可比较工作量。门闩按每次获取计（可能重试），跨阶段请求按各hook当时的阶段归属，不能以成交成功数直接除总SQL推导固定单笔SQL。CPU仅应用进程，100%约为一个核，不包含PG/驱动。固定读CPU秒按1秒资源样本与0.5秒phase采样窗口重叠积分（默认窗口内CPU均匀），再除固定请求数；包含少量阶段边界/后台开销，仅近似，不是精确请求CPU归因。','','PG实际测试（工作目录 `/tmp/sunyunbo/thccb-fx-read-checkpoint-20261017/backend`）：`TEST_PG_DATABASE_URL=\'postgresql+asyncpg://sunyunbo@/fx_dependency_context_test?host=/tmp/sunyunbo/fx-pool-incident-20261017/socket&port=55489\' /data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest -q -m pg tests/pg/test_pg_trading_context.py`，5 passed in 2.87s，非skip。独立测试在before seed准备阶段完成，未与固定测量/混合负载重叠。未运行全量或浏览器交互测试。','','执行记录：harness/是本次实际脚本快照（修订说明见harness/REVISIONS.md），依赖旧/tmp harness及包含未提交融资diff的基线；固定副本目录与测试库现已存在。再次执行须保留相同融资diff，换新目录/库名并调整路径，不能直接开箱重跑；reproduce.sh仅记录本次编排。启动同设置隔离PG后，实际入口为`backend/venv/bin/python /tmp/sunyunbo/fx-dependency-query-ab-20261018/run.py`；prepare.py从指定dirty worktree构造副本，adapt.py修改hooks，read_driver.py固定读，build_report.py聚合。结果harness/保存本次脚本，不含backend副本或token。每版raw：http/summary.json、http/requests.jsonl、resources.jsonl、sql-counts.json、dependency-sql.json、gates.json、gate-queue.jsonl、backend.log。新建库保留；服务和本次启动的PG已关闭，令牌已删除。']
(root/'report.md').write_text('\n'.join(lines)+'\n')
