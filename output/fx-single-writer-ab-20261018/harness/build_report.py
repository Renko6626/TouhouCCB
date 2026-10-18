"""Render only existing A/B data; no services, queries, workload or dependencies."""
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

root = Path('/data/sunyunbo/www/TouhouCCB/output/fx-single-writer-ab-20261018')
a = json.loads((root / 'analysis.json').read_text())
load = lambda p: json.loads(p.read_text())
fmt = lambda v: '—' if v is None else f'{v:.3f}'
variants = ('before', 'after')
stages = ('readheavy', 'readonly', 'normal', 'active', 'stress')
by = {v: {s['name']: s for s in a[v]['stages']} for v in variants}
lines = ['# FX 唯一 writer 最终 A/B 结果', '',
'本轮确认了普通账户读取与市场执行排队的解耦。stress 下，versions P99 从 **5653ms 降到 284ms**，成交后 context P99 从 **5690ms 降到 341ms**。写入容量仍受限：两边均触发“30 秒窗口成功交易 P99 >2000ms”的既定保护，**burst 均未执行**。after 的 stress 实际成功吞吐为 10.909/s，before 为 10.110/s；这不表示通过了名义 25 cycles/s 的容量目标。', '',
'after 的成交延迟也有回退：readheavy 成交 P50 58→85ms，normal 62→85ms，active 124→151ms；stress 成交 P50 4555→8399ms、P99 9816→10021ms。迁移带来的主要收益是读取在写入积压时保持可用，不能描述成整体交易延迟或容量全面改善。', '',
'## 实验来源与范围', '',
'- before：`330f4e683ec5564dea6ea59983351c3c8e128d9d`；after：`6c7675722cda265bd205dced6ce0f939ae7ac769`，均为 committed backend 的 git archive。',
'- 单机顺序运行一次 before→after；全新隔离库 `fx_single_writer_before_test` / `fx_single_writer_after_test`，相同初始化 helper、seed 20261017、100 用户、200 SSE（public/private 各100）、cash/finance/short 60/20/20、pool 10+20、单 Uvicorn。没有生产数据或产品代码修改。',
'- HTTP/readheavy/resource sampler 与上一轮字节相同：readheavy 60s、1200 固定读+30定时cycle；readonly 30s；normal 60s；active 45s；stress 30s；原 burst 观察10s。其余角色准备、历史请求、版本探测和停止保护均沿用。每个cycle包含真实server quote，采用完成后重新安排的原时序，不积累追赶任务；过载时实际发起率会下降。',
'- 两个实验源码的 SSE `MAX_PER_IP=200`（每topic/IP），支持同一loopback IP的此负载；主工作树另有生产限制10，整合保留10。本结果不说明生产同IP用户容量，也不表示生产IP策略已修改。',
'- PostgreSQL14 `fsync=on`、`synchronous_commit=on`、I/O/WAL计时开启、`pg_stat_statements`启用；没有profiler或额外负载。', '',
'## 各阶段实际结果', '',
'成功交易/s = 实际成功成交数 / driver 实际阶段时长。时长包含原有阶段末尾观察与在途请求排空；不是单取配置的30/45/60秒。HTTP统计排除cycle计数和SSE流；quote_blocked表示报价业务不允许继续成交，并非HTTP错误或成功交易。所有下表已完成HTTP请求均为ok，429/5xx/network/timeout合计0；不能据此忽略延迟保护。', '',
'|阶段|版本|实际秒|发起cycles|成功成交|成功交易/s|完成HTTP|HTTP错误|quote_blocked|结果|',
'|---|---|---:|---:|---:|---:|---:|---:|---:|---|']
for name in stages:
    for v in variants:
        s=by[v][name];t=s['stage'];errors=sum(n for k,n in s['http_results'].items() if k!='ok')
        lines.append(f"|{name}|{v}|{t['duration_s']:.3f}|{t['attempted_trade_cycles']}|{t['successful_trades']}|{t['successful_trades_per_s']:.3f}|{t['completed_requests']}|{errors}|{s['cycle_results'].get('quote_blocked',0)}|{'延迟保护停止' if name=='stress' else '完成'}|")
lines += ['', '两边readheavy均完成300×4=1200固定读取和30成功成交，合计1350个完成HTTP请求。normal/active实际成功吞吐基本持平；stress在两个保护截断和不同排空时长下观察到约7.9%的成功吞吐差异。只做了一次顺序实验，不作稳定容量或统计显著性结论。', '',
'|阶段|版本|成功成交P50 ms|P95 ms|P99 ms|', '|---|---|---:|---:|---:|']
for name in ('readheavy','normal','active','stress'):
    for v in variants:
        d=by[v][name]['trade_latency'];lines.append(f"|{name}|{v}|{d['p50_ms']:.3f}|{d['p95_ms']:.3f}|{d['p99_ms']:.3f}|")
lines += ['', '## 读取延迟', '', '均为成功响应分布；n为before→after样本数。行情snapshot与账户context分开列出，小样本尾部和回退也保留。', '',
'|阶段|读取|n before→after|P95 ms before→after|P99 ms before→after|', '|---|---|---|---|---|']
readnames={'fixed_context':'固定context','fixed_versions':'固定versions','fixed_prices':'固定prices','fixed_snapshot':'固定行情snapshot','versions':'周期versions','context_after_trade':'成交后context','snapshot_after_trade':'成交后行情snapshot','prices_repair':'prices repair'}
for name in stages:
    pairs={v:{r['name']:r['latency_by_result'].get('ok',{}) for r in by[v][name]['read_requests']} for v in variants}
    for key in readnames:
        if name=='readheavy' and not key.startswith('fixed_'):continue
        b,c=pairs['before'].get(key,{}),pairs['after'].get(key,{})
        if not b and not c:continue
        lines.append(f"|{name}|{readnames[key]}|{b.get('count',0)}→{c.get('count',0)}|{fmt(b.get('p95_ms'))}→{fmt(c.get('p95_ms'))}|{fmt(b.get('p99_ms'))}→{fmt(c.get('p99_ms'))}|")
lines += ['', '读取改善集中在active/stress的versions/context。回退包括readheavy固定context P99 83→93ms、固定snapshot 26→44ms，readonly versions P99 44→80ms，以及stress成交后snapshot P99 138→171ms；不能声称所有读取分位数都改善。公共精确历史协议仍保留FOR SHARE，可能等待数据库；本实验不证明历史组合SSE完全免数据库等待。', '',
'## Writer队列与执行', '',
'before没有FX actor，记录为absent/unavailable；after已附加observer。下面只列after实测，不以before=0比较。queue wait是成功接受enqueue至dequeue返回；execution是dequeue返回至下一次get调用或consumer退出，包含handler、commit/发布/通知、future处理及恢复，不含空闲queue.get。各列分位数来自不同样本排序，**不能把queue P99与execution P99相加当作HTTP P99**。', '',
'|阶段|命令|n|排队均值 ms|排队P95 ms|排队P99 ms|执行均值 ms|执行P95 ms|执行P99 ms|', '|---|---|---:|---:|---:|---:|---:|---:|---:|']
for name in stages:
    rows=by['after'][name]['fx_writer_timings']
    for command in sorted({r['command'] for r in rows}):
        q=next(r for r in rows if r['command']==command and r['kind']=='queue_wait');e=next(r for r in rows if r['command']==command and r['kind']=='execution')
        lines.append(f"|{name}|{command}|{q['count']}|{q['mean_ms']:.3f}|{q['p95_ms']:.3f}|{q['p99_ms']:.3f}|{e['mean_ms']:.3f}|{e['p95_ms']:.3f}|{e['p99_ms']:.3f}|")
lines += ['', 'stress净交易排队P99约9863ms，执行P99约243ms；与CPU接近单核占满同时出现，支持“普通读已解耦、写队列仍积压”的判断。没有profiler，不能进一步断言具体CPU函数或把全部积压归因于某个SQL/锁。phase通过原50ms轮询更新，紧邻阶段边界的少量SQL/actor样本可能归到相邻phase，例如readheavy actor为29条而HTTP成功30条；不代表丢失成交。', '',
'## SQL、资源与锁观测', '',
'SQL cursor数量按整个服务器phase累计，包含阶段末尾排空；资源/PG采样窗口在stress截于保护时刻。两者时间范围不同，不能直接混除计算每请求数据库成本。CPU为进程样本均值，100%约一个核心；PG CPU为backend采样差量近似，PG执行ms为pg_stat_statements阶段快照差量。', '',
'|阶段|版本|SQL cursor总数|App CPU %|pool峰值|eventloop P99 ms|PG CPU近似s|PG执行ms|', '|---|---|---:|---:|---:|---:|---:|---:|']
for name in stages:
    for v in variants:
        s=by[v][name];lines.append(f"|{name}|{v}|{s['sql_total']}|{s['cpu_mean_percent_approx']:.2f}|{s['pool_peak_phase_sampled']}|{s['event_loop_lag'].get('p99_ms',0):.3f}|{s['pg_cpu_s_approx']:.3f}|{s.get('pg_delta',{}).get('exec_total_ms',0):.3f}|")
lines += ['', '|阶段|HTTP business SQL before→after|after writer后台SQL|live-read SQL before→after|', '|---|---:|---:|---:|']
for name in stages:
    b,c=by['before'][name]['sql_by_family'],by['after'][name]['sql_by_family']
    lines.append(f"|{name}|{b.get('business_quote_or_write',0)}→{c.get('business_quote_or_write',0)}|{c.get('background:fx_writer',0)}|{b.get('live_market_read',0)}→{c.get('live_market_read',0)}|")
lines += ['', 'writer SQL现记为background:fx_writer，不能把HTTP business SQL减少全部当作数据库工作消失。总SQL在normal/active略降，stress随实际成功成交数增加而上升。目录、my-trades join、业务报价/写入、pool_version/history SQL继续保留；此次迁移没有把所有读取变为零SQL。', '',
'以下为User FOR UPDATE的**cursor查询总耗时**，不是纯锁等待。before按两个HTTP路由分开，after actor行包含所有命令，样本组合不同，不做同分布锁性能比较。', '',
'|阶段|版本/归属|n|均值ms|P95 ms|P99 ms|最大ms|', '|---|---|---:|---:|---:|---:|---:|']
for name in ('readheavy','normal','active','stress'):
    for v in variants:
        for r in by[v][name]['lock_query_elapsed']:
            if r['kind']!='account_user_for_update' or (v=='before' and not r['route'].startswith('POST')) or (v=='after' and r['route']!='background:fx_writer'):continue
            label='actor所有命令' if v=='after' else 'cover' if r['route'].endswith('/short/cover') else 'net/trades'
            lines.append(f"|{name}|{v}/{label}|{r['count']}|{r['mean_ms']:.3f}|{r['p95_ms']:.3f}|{r['p99_ms']:.3f}|{r['max_ms']:.3f}|")
lines += ['', '|阶段|版本|观察到Lock backend样本|其中User FOR UPDATE样本|blocking PID非空样本|', '|---|---|---:|---:|---:|']
for name in stages:
    for v in variants:
        s=by[v][name];waits=s['pg_lock_wait_samples'];lines.append(f"|{name}|{v}|{sum(r['backend_samples'] for r in waits)}|{sum(r['backend_samples'] for r in waits if r['observed_query_kind']=='account_user_for_update')}|{s['pg_blocked_backend_samples']}|")
lines += ['', 'PG wait为0.5秒离散采样，0样本只表示未采到；不是证明没有User锁等待。精确逐查询锁等待时长没有测得。before stress GATE waiting峰值133、net交易等待P99约5451ms、context等待P99约5501ms；after GATE已删除，字段为null/absent，不虚构零计时。', '',
'## SSE与可见性边界', '',
'global SSE指标含smoke/connect/角色准备及各阶段，event age依赖客户端/服务端时钟；下面不是单独stress分布。', '',
'|类型|版本|首帧n|首帧P99 ms|收到frames|payload bytes|event age P95/P99 ms|接收间隔P99 ms|', '|---|---|---:|---:|---:|---:|---|---:|']
for kind in ('public','private'):
    for v in variants:
        d=a[v]['sse_global'][kind];age=d['event_age_ms_clock_dependent'];lines.append(f"|{kind}|{v}|{d['first_frame']['count']}|{d['first_frame']['p99_ms']:.3f}|{d['counts']['frames']}|{d['counts']['payload_bytes']}|{age['p95_ms']:.3f}/{age['p99_ms']:.3f}|{d['receive_interval']['p99_ms']:.3f}|")
lines += ['', '两边直到active阶段末均保持public/private各100连接。stress保护使driver关闭SSE，阶段末connected=0；保护原因是成交P99，不能把这个0当成先发生的服务端断流。readonly公共SSE无交易帧符合该阶段无交易；私人仍有心跳。全量分阶段帧/字节留在summary/analysis中。', '',
'继承的成交到首个observer可见延迟未产生有效匹配：normal/active/stress的visibility count均为0，unmatched分别为before 156/324/341、after 155/327/382。因此不能量化逐笔成交到客户端可见延迟，也不能用其空值声称零延迟。保留该限制，没有扩大或修改测量平台。', '',
'## 保护与统计时间边界', '',
'|版本|stress起点 上海时间|保护前秒数|保护后至阶段结束秒数|整阶段秒数|采样资源窗口秒数|未执行|', '|---|---|---:|---:|---:|---:|---|']
for v in variants:
    p=root/v;start=load(p/'stress-started.json')['t'];protect=load(p/'protection.json')['t'];end=load(p/'stress-ended.json')['t'];s=by[v]['stress'];local=datetime.fromtimestamp(start,timezone(timedelta(hours=8))).isoformat(timespec='milliseconds')
    lines.append(f"|{v}|{local}|{protect-start:.3f}|{end-protect:.3f}|{s['stage']['duration_s']:.3f}|{s['measurement_end']-s['measurement_start_approx']:.3f}|burst；orders配置0|")
lines += ['', '两边保护原因相同：`30s window: successful trade P99 exceeds 2000ms`。driver退出码各1是保护停止；外层run.py已按finally清理并完成两边，退出码0不等于性能容量验收通过。HTTP/SQL/actor分布通常含排空，保护窗口是滚动30秒；不要把整阶段P99当成触发瞬间的同一统计样本。', '',
'## 验证、清理与原始证据', '',
'已执行一次指定run.py --start-authorized，再执行analyze.py；归档/哈希、check_observer.py --commit最终SHA、py_compile均通过。两边pg-monitor-errors均[]，backend日志均未出现Traceback、QueuePool limit或CancelledError。未运行浏览器或交互流程测试，未新增产品测试或生产改动。', '',
'清理核验：本轮run/driver/monitor/count_server进程为[]，18543无监听，before/after的private/tokens.json均不存在；主agent独立核实后已停止隔离PG。本agent没有停止PG。', '',
'未测项目：burst因保护均未执行；orders原配置0；无重复试验/统计置信区间；新库历史较浅；无跨市场性能、跨进程部署、外部网络延迟结论；未测精确逐查询lock wait与逐笔SSE visibility。公共精确历史仍可等DB。资金正确性和主工作树整合验证由主agent另外报告，不把本压测当作资金验收。', '',
'复现命令：', '', '```bash',
'/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python /tmp/sunyunbo/fx-single-writer-ab-20261018/run.py --start-authorized',
'/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python /tmp/sunyunbo/fx-single-writer-ab-20261018/analyze.py',
'```', '',
'已有结果目录/数据库不得覆盖；本轮不重跑。实际完整driver参数见各variant `actual-command.json`。轻量observer只增加时钟读数、字典/counter/sample操作及actor context标签，无额外SQL/task/await；overhead未独立benchmark或扣除，after承担其适用的actor计时成本，共有观测两边一致。', '',
'原始证据：[analysis.json](analysis.json)、[before HTTP summary](before/http/summary.json)、[after HTTP summary](after/http/summary.json)、[before protection](before/protection.json)、[after protection](after/protection.json)、[before源码hash](before-source.json)、[after源码hash](after-source.json)、[harness manifest](harness-manifest.json)、[harness镜像](harness/)、[准备记录](harness/PREPARED.md)。每边同时保留requests.jsonl、phases.jsonl、PG活动/SQL快照、SQL分类、body计数、资源/eventloop、query elapsed、writer timings/counters、完整日志；不要只看本报告舍入值。', '']
(root/'report.md').write_text('\n'.join(lines))
print('wrote Chinese report',root/'report.md')
