import json,collections
from pathlib import Path
root=Path('/data/sunyunbo/www/TouhouCCB/output/fx-single-writer-ab-20261018');load=lambda p:json.loads(p.read_text())
def rows(p):return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
def dist(v):
 if not v:return {'count':0}
 s=sorted(v);n=len(s);return dict(count=n,mean_ms=sum(s)/n,p50_ms=s[int((n-1)*.5)],p95_ms=s[int((n-1)*.95)],p99_ms=s[int((n-1)*.99)],max_ms=s[-1])
results={}
for variant in ('before','after'):
 p=root/variant;raw=load(p/'http/summary.json');sql=load(p/'sql-classifications.json');deps=load(p/'dependency-sql.json');gates=load(p/'gates.json');instrumentation=load(p/'instrumentation.json');lock_queries=load(p/'lock-query-elapsed.json');writer_timings=load(p/'fx-writer-timings.json');writer_counters=load(p/'fx-writer-counters.json');queue=rows(p/'gate-queue.jsonl');resources=rows(p/'resources.jsonl');lag=load(p/'eventloop-lag.json');pg=rows(p/'pg-activity.jsonl');snapshots=rows(p/'pg-snapshots.jsonl');body=load(p/'body-counts.json');protect=load(p/'protection.json') if (p/'protection.json').exists() else None
 info={'commit':load(root/(variant+'-source.json'))['commit'],'abort':raw['abort_reason'],'protection':protect,'sse_global':raw['sse'],'stages':[],'all_http_requests':raw['requests'],'pg_monitor_errors':load(p/'pg-monitor-errors.json'),'pool_peak_all':max((r['pool_peak_since_sample'] for r in resources),default=0),'instrumentation':instrumentation,'scope':{'market_directory':'retained DB','personal_trade_join':'retained DB','business_quote_or_write':'retained DB','driver_control_sql':'excluded from cursor counts'}}
 for stage in raw['stages']:
  name=stage['name']
  if name not in ('readheavy','readonly','normal','active','stress','burst'):continue
  q=[x for x in queue if x['phase']==name];lo=q[0]['t'] if q else 0;hi=q[-1]['t'] if q else 0
  if protect and protect['phase']==name:hi=min(hi,protect['t'])
  rr=[x for x in resources if lo<=x['t']<=hi];aa=[x for x in pg if lo<=x['t']<=hi];rq=[x for x in raw['requests'] if x['phase']==name];ss=[x for x in sql if x['phase']==name];gg=[x for x in gates if x['phase']==name]
  trade=next((x for x in rq if x['name']=='trade'),{'results':{},'latency_by_result':{}});cycle=next((x['results'] for x in rq if x['name']=='cycle'),{})
  counts=collections.Counter()
  for x in rq:
   if x['name']!='cycle' and not x['name'].startswith('sse_'):counts.update(x['results'])
  category=collections.Counter();family=collections.Counter()
  for x in ss:category[x['category']]+=x['executions'];family[x['family']]+=x['executions']
  r={'name':name,'stage':stage,'trade_results':trade['results'],'cycle_results':cycle,'trade_latency':trade['latency_by_result'].get('ok',{}),'http_results':dict(counts),'cpu_mean_percent_approx':sum(x['app_cpu_percent'] for x in rr)/len(rr) if rr else None,'resources_samples':len(rr),'pool_peak_phase_sampled':max((x['pool_peak_since_sample'] for x in rr),default=0),'queue_peak':max((x['waiting'] for x in q if x['t']<=hi and x.get('waiting') is not None),default=None),'event_loop_lag':dist([x['lag_ms'] for x in lag if lo<=x['t']<=hi]),'gate_by_route':gg,'lock_query_elapsed':[x for x in lock_queries if x['phase']==name],'fx_writer_timings':[x for x in writer_timings if x['phase']==name],'fx_writer_counters':[x for x in writer_counters if x['phase']==name],'sql_total':sum(x['executions'] for x in ss),'sql_by_category':dict(category),'sql_by_family':dict(family),'sql_classifications':ss,'read_requests':[x for x in rq if x['name'].startswith('fixed_') or x['name'].startswith('context_') or x['name'] in ('versions','prices_repair','snapshot_after_trade')],'history_http':[x for x in rq if x['name'].startswith('chart_') or x['name']=='history_segment'],'body_counts':[x for x in body if x['phase']==name],'trade_visibility':raw.get('trade_visibility',{}).get(name),'pg_cpu_s_approx':sum(x['pg_cpu_s'] for x in aa),'measurement_start_approx':lo,'measurement_end':hi}
  for req in r['read_requests']:
   route={'fixed_context':'GET /api/v1/user/trading-context','fixed_versions':'GET /api/v1/user/trading-versions','fixed_prices':'GET /api/v1/user/trading-prices','fixed_snapshot':'GET /api/v1/fx/pairs/:id/snapshot'}.get(req['name'])
   if route:
    executions=sum(x['executions'] for x in ss if x['route']==route);n=sum(req['results'].values());r.setdefault('fixed_read_sql',[]).append(dict(name=req['name'],request_count=n,route=route,route_sql_executions=executions,route_sql_per_fixed_request_upper_bound=executions/n if n else None,warning='route includes any trade-cycle refreshes on the same route; context/snapshot are not exclusively fixed reads',dependency_union=sum(x['executions'] for x in deps if x['phase']==name and x['route']==route and x['kind']=='dependency_union')))
  waits=collections.Counter();blocked=[];lock_wait_samples=collections.Counter()
  for x in aa:
   for a in x['activity']:
    waits[(a['state'],a['wait_event_type'],a['wait_event'])]+=1
    if a['blocking_pids']:blocked.append(a)
    if a['wait_event_type']=='Lock':lock_wait_samples[(a.get('observed_query_kind','unclassified'),a['wait_event'])]+=1
  r['pg_lock_wait_samples']=[{'observed_query_kind':k,'event':e,'backend_samples':n} for (k,e),n in lock_wait_samples.items()];r['pg_lock_wait_duration_ms']=None;r['pg_lock_wait_warning']='0.5s observed wait-state sampling; not exact per-query lock-wait durations';r['pg_activity_counts']=[dict(state=s,type=t,event=e,samples=n) for (s,t,e),n in waits.most_common()];r['pg_blocked_backend_samples']=len(blocked);r['pg_blocked_queries']=dict(collections.Counter(a['query_id'] for a in blocked))
  startidx=next((i for i,x in enumerate(snapshots) if x['label']=='phase_entry:'+name),None)
  endshot=next((x for x in snapshots if name=='stress' and x['label']=='stress_protection_or_end'),None)
  if startidx is not None:
   a=snapshots[startidx];b=endshot or (snapshots[startidx+1] if startidx+1<len(snapshots) else None)
   if b:
    old={x['queryid']:x for x in a['queries']};delta=[]
    for x in b['queries']:
     previous=old.get(x['queryid'],{});d={k:float(x[k])-float(previous.get(k,0)) for k in ('calls','total_exec_time','rows','shared_blks_hit','shared_blks_read','blk_read_time','blk_write_time','wal_bytes')}
     if d['calls']>0:delta.append(dict(queryid=x['queryid'],query=x['query'],**d))
    r['pg_delta']={'start':a['t'],'end':b['t'],'end_label':b['label'],'queries':sorted(delta,key=lambda x:x['total_exec_time'],reverse=True),'exec_total_ms':sum(x['total_exec_time'] for x in delta),'wal_clusterwide':{k:float(b['wal'][k])-float(a['wal'][k]) for k in ('wal_sync_time','wal_write_time','wal_sync','wal_write')}}
  info['stages'].append(r)
 log=(p/'backend.log').read_text();info.update(pool_error_mentions=log.count('QueuePool limit'),tracebacks=log.count('Traceback (most recent call last)'),cancelled_mentions=log.count('CancelledError'),unrun_stages=[s for s in ('readheavy','readonly','normal','active','stress','burst') if s not in [x['name'] for x in info['stages']]])
 results[variant]=info
(root/'analysis.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({v:{'abort':r['abort'],'stages':[{k:s[k] for k in ('name','trade_results','sql_total','cpu_mean_percent_approx','queue_peak','pool_peak_phase_sampled')} for s in r['stages']]} for v,r in results.items()},ensure_ascii=False,indent=2))
