import json
from pathlib import Path
root=Path('/data/sunyunbo/www/TouhouCCB/output/sse-reuse-ab-20261018')
d=json.loads((root/'analysis.json').read_text())
by={v:{s['name']:s for s in data['stages']} for v,data in d.items()}
lines=['# 私人SSE消息构造复用 A/B','',
'本轮只改变私人SSE消息构造/编码复用：相同pricing订阅子集一轮只构造编码一次，账户/订单通知一次发布编码一次；既有队列逐连接发送，全部事件/FIFO/鉴权/15秒检查/队列满关闭不变。公共SSE原已共享bytes，本轮未修改。writer和风险计算未改。','',
f"固定来源：before `{d['before']['commit']}`；after `{d['after']['commit']}`。各自git archive到独立目录、全新临时数据库sse_reuse_before_test/after_test。100用户、200SSE、cash/finance/short60/20/20；seed20261017，pool10+20，单Uvicorn；沿用上一轮完整负载和保护，无profiler。两边实验源每topic/IP200以允许同一loopback100用户；主工作区每IP10保留。",'',
'## 实际阶段结果','',
'成功交易/s按实际成功HTTP成交数除以driver阶段时长，包含原阶段观察及在途排空。名义25cycles/s是合计请求周期负载，不是成功交易/s。一次顺序before→after没有统计显著性/稳定容量保证。','',
'|阶段|版本|实际秒|成功成交|成功交易/s|成交P50/P95/P99 ms|CPU %|pool峰值|HTTP结果|',
'|---|---|---:|---:|---:|---|---:|---:|---|']
names=[n for n in ('readheavy','readonly','normal','active','stress','burst') if any(n in by[v] for v in d)]
def num(x):return '—' if x is None else f'{x:.3f}'
for name in names:
    for v in d:
        s=by[v].get(name)
        if s is None:continue
        t=s['stage'];lat=s['trade_latency']
        lines.append(f"|{name}|{v}|{t['duration_s']:.3f}|{t['successful_trades']}|{t['successful_trades_per_s']:.3f}|{' / '.join(num(lat.get(k)) for k in ('p50_ms','p95_ms','p99_ms'))}|{num(s['cpu_mean_percent_approx'])}|{s['pool_peak_phase_sampled']}|{json.dumps(s['http_results'],ensure_ascii=False)}|")
lines+=['','## 保护停止与未运行项','']
for v,data in d.items():
    lines.append(f"- {v}：abort={data['abort']!r}；未执行阶段={data['unrun_stages']}；PGmonitor errors={data['pg_monitor_errors']}；QueuePool日志={data['pool_error_mentions']}；traceback={data['tracebacks']}。")
lines+=['','保护触发后队列排空数据仍保留；停止后SSE连接数变0不自动代表先发生了异常断流。成功响应之外的拒绝、超时、结果未知不得计入成功吞吐。','',
'## 读取P99','',
'|阶段|读取|before样本/P99 ms|after样本/P99 ms|','|---|---|---|---|']
for name in names:
    maps={v:{r['name']:r for r in by[v].get(name,{}).get('read_requests',[])} for v in d}
    for req in ('fixed_context','fixed_versions','fixed_prices','fixed_snapshot','versions','context_after_trade','snapshot_after_trade'):
        if not any(req in maps[v] for v in d):continue
        cells=[]
        for v in d:
            lat=maps[v].get(req,{}).get('latency_by_result',{}).get('ok',{})
            cells.append(str(lat.get('count',0))+' / '+num(lat.get('p99_ms')))
        lines.append('|'+name+'|'+req+'|'+'|'.join(cells)+'|')
lines+=['','## 推送工作量、SQL与writer','',
'|阶段|版本|私人SSE body帧/字节|公共SSE body帧/字节|SQL总数|writer NetTrade排队均值/P99 ms|执行均值/P99 ms|','|---|---|---|---|---:|---|---|']
for name in names:
    for v in d:
        s=by[v].get(name)
        if not s:continue
        bc={(r['route'],r['kind']):r['count'] for r in s['body_counts']}
        def wire(route):return f"{bc.get((route,'frames'),0)} / {bc.get((route,'bytes'),0)}"
        timings={r['kind']:r for r in s['fx_writer_timings'] if r['command']=='NetTrade' and r['boundary']!='skipped_already_done'}
        def timing(k):return num(timings.get(k,{}).get('mean_ms'))+' / '+num(timings.get(k,{}).get('p99_ms'))
        lines.append(f"|{name}|{v}|{wire('GET /api/v1/stream/trading')}|{wire('GET /api/v1/fx/stream/:id')}|{s['sql_total']}|{timing('queue_wait')}|{timing('execution')}|")
lines+=['',
'SQL按整个server phase累计（含排空）；CPU/PG资源采样在stress保护时刻截断，时间范围不同，不能直接混除算每请求成本。writer执行是出队至下次取命令，包括提交/发布/通知/future处理，排队与执行分位数不能相加当HTTP分位数。SQL归后台writer，不能只看HTTP SQL。SSE body计数是服务器发送边界，不证明客户端逐笔可见时延；继承driver的visibility匹配若为空也不能当零延迟。公共精确history保留FOR SHARE，仍可能等DB。','',
'本轮不做CPU函数归因：低成本资源观测相同，未开启profile。数据复用确已通过行为验证，整体吞吐/CPU收益以表内实际数据为准，不能由少一次编码推导容量提高。仅单交易目标市场，不外推跨市场性能。','',
'代码与验证见 validation/；writer只读SQL方案见 [writer-sql-audit.md](writer-sql-audit.md)，尚未实施。原始证据为 analysis.json、before/after HTTP summary与requests、PG快照、writer timings、body-counts、资源和完整日志；源码hash与harness镜像均保留。清理状态见 cleanup.json。','']
(root/'report.md').write_text('\n'.join(lines))
print(root/'report.md')
