import json,collections,math,hashlib,shutil
from pathlib import Path
root=Path('/data/sunyunbo/www/TouhouCCB/output/fx-stress-profile-20261018');harness=Path(__file__).resolve().parent
load=lambda p:json.loads(p.read_text())
def rows(p):return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []
def dist(v):
 if not v:return {'count':0}
 s=sorted(v);n=len(s)
 return dict(count=n,total_ms=sum(s),mean_ms=sum(s)/n,p50_ms=s[int((n-1)*.5)],p95_ms=s[int((n-1)*.95)],p99_ms=s[int((n-1)*.99)],max_ms=s[-1])
def pct(c):
 total=sum(c.values());return [dict(name=k,samples=v,percent=100*v/total if total else 0) for k,v in c.most_common()]
def category(f):
 p=f.get('file','').replace('\\','/');name=f.get('name','')
 if '/sqlalchemy/' in p:return 'SQLAlchemy'
 if '/asyncpg/' in p or 'asyncpg' in name:return 'asyncpg'
 if '/pydantic/' in p or 'pydantic' in name:return 'Pydantic'
 if '/json/' in p or '_json' in name:return 'JSON'
 if p.endswith('/profile_server.py'):return 'instrumentation'
 if '/fastapi/' in p or '/starlette/' in p:return 'FastAPI/Starlette'
 if '/asyncio/' in p or '/anyio/' in p:return 'async scheduling'
 if p.endswith('/app/services/fx/amm.py'):return 'AMM math'
 if p.endswith('/app/services/credit/risk.py'):return 'risk Python'
 if '/app/' in p:return 'other application Python'
 if '/uvicorn/' in p:return 'Uvicorn'
 if not p:return 'native or unavailable file'
 return 'other Python/native caller'
result={}
for variant in ('control','profile'):
 p=root/variant;raw=load(p/'http/summary.json');resources=rows(p/'resources.jsonl');activity=rows(p/'pg-activity.jsonl');snapshots=rows(p/'pg-snapshots.jsonl');protect=load(p/'protection.json') if (p/'protection.json').exists() else None
 start=load(p/'stress-started.json')['t'] if (p/'stress-started.json').exists() else None;end=protect['t'] if protect else load(p/'stress-ended.json')['t'];stage=next((s for s in raw['stages'] if s['name']=='stress'),None)
 info={'abort':raw['abort_reason'],'stress_start':start,'load_stop':end,'stress_window_s':end-start if start else 0,'stress_stage':stage,'protection':protect,'stress_stage_return':load(p/'stress-ended.json') if (p/'stress-ended.json').exists() else None,'all_stages':raw['stages'],'monitor_errors':load(p/'monitor-errors.json'),'pg_monitor_errors':load(p/'pg-monitor-errors.json'),'pool_peak_all':max(x['pool_peak_since_sample'] for x in resources)}
 if not start:info['error']='no stress';result[variant]=info;continue
 rr=[x for x in resources if start<=x['t']<=end];duration=sum(x['elapsed_s'] for x in rr);aa=[x for x in activity if start<=x['t']<=end]
 info['stress_resources']={'samples':len(rr),'main_thread_cpu_fraction':sum(x.get('main_thread_cpu_s',0) for x in rr)/sum(x['cpu_s'] for x in rr) if rr and 'main_thread_cpu_s' in rr[0] else None,'app_cpu_s_approx':sum(x['cpu_s'] for x in rr),'app_cpu_percent_approx':100*sum(x['cpu_s'] for x in rr)/duration if duration else 0,'pool_peak':max((x['pool_peak_since_sample'] for x in rr),default=0),'gate_queue_peak':max((x['gate_waiting'] for x in rr),default=0),'pg_cpu_s_approx':sum(x['pg_cpu_s'] for x in aa),'pg_samples':len(aa)}
 waits=collections.Counter();blocking=collections.Counter()
 for x in aa:
  for a in x['activity']:
   waits[(a['state'],a['wait_event_type'],a['wait_event'])]+=1
   if a['blocking_pids']:blocking[(a['wait_event_type'],a['wait_event'])]+=1
 info['pg_activity_counts']=[dict(state=s,type=t,event=e,samples=n) for (s,t,e),n in waits.most_common()];info['pg_blocked_backend_samples']=sum(blocking.values())
 info['requests']=[r for r in raw['requests'] if r['phase']=='stress'];info['quote_http_completions']=sum(sum(r['results'].values()) for r in info['requests'] if r['name'].startswith('quote_'));info['write_http_completions']=sum(sum(r['results'].values()) for r in info['requests'] if r['name']=='trade');info['bodies']=load(p/'body-counts.json')
 info['queue_timeline']=[dict(t=x['t'],relative_s=x['t']-start,waiting=x['gate_waiting'],holders=x['gate_holders'],pool=x['pool_checked_out'],after_stop=x['t']>end) for x in resources if x['t']>=start]
 after=[x for x in resources if x['t']>end];zero=next((x['t']-end for x in after if x['gate_waiting']==0 and x['gate_holders']==0),None)
 info['post_stop_queue_observation']={'observed_seconds':after[-1]['t']-end if after else 0,'first_zero_queue_and_holders_after_stop_s':zero,'natural_recovery_claim':False,'reason':'driver stop and task cancellation/phase drain; not a natural unconstrained recovery experiment'}
 events=load(p/'timed-events.json');enclosed=[e for e in events if e['start']>=start and e['end']<=end];group=collections.defaultdict(list)
 for e in enclosed:group[(e['route'],e['name'])].append(e['ms'])
 info['timings_enclosed_window']=[dict(route=r,name=n,**dist(v)) for (r,n),v in group.items()];info['cross_boundary_timed_events']=len(events)-len(enclosed)
 gates=load(p/'gate-events.json');complete=[g for g in gates if g['requested']>=start and g['released']<=end];detailed=[g for g in complete if g['detailed']]
 info['gate_by_route']=[dict(route=r,wait=dist([g['wait_ms'] for g in complete if g['route']==r]),held=dist([g['held_ms'] for g in complete if g['route']==r]),detailed=sum(g['detailed'] for g in complete if g['route']==r)) for r in sorted({g['route'] for g in complete})]
 info['gate_events']={'recorded':len(gates),'fully_enclosed':len(complete),'cross_boundary_or_drain':len(gates)-len(complete),'wait':dist([g['wait_ms'] for g in complete]),'held':dist([g['held_ms'] for g in complete]),'detailed_count':len(detailed),'decomposition':{k:dist(vals) for k,vals in {'held':[g['held_ms'] for g in detailed],'cursor_wait':[g['sql_s']*1000 for g in detailed],'commit_driver':[g['commit_s']*1000 for g in detailed],'remaining_inclusive':[max(0,g['held_ms']-1000*g['sql_s']-1000*g['commit_s']) for g in detailed],'flush_inclusive_overlaps_cursor':[g['flush_s']*1000 for g in detailed]}.items()}}
 a=next((s for s in snapshots if s['label']=='stress_entry'),None);b=next((s for s in snapshots if s['label']=='stress_protection_or_end'),None)
 if a and b:
  old={q['queryid']:q for q in a['queries']};deltas=[]
  for q in b['queries']:
   prev=old.get(q['queryid'],{});d={k:float(q[k])-float(prev.get(k,0)) for k in ('calls','total_exec_time','total_plan_time','rows','shared_blks_hit','shared_blks_read','blk_read_time','blk_write_time','wal_bytes')}
   if d['calls']>0:deltas.append(dict(queryid=q['queryid'],query=q['query'],**d))
  deltas.sort(key=lambda x:x['total_exec_time'],reverse=True)
  info['pg_delta']={'snapshot_start':a['t'],'snapshot_end':b['t'],'start_delay_ms':1000*(a['t']-start),'end_delay_ms':1000*(b['t']-end),'queries':deltas,'exec_total_ms':sum(q['total_exec_time'] for q in deltas),'read_io_ms':sum(q['blk_read_time'] for q in deltas),'write_io_ms':sum(q['blk_write_time'] for q in deltas),'wal_clusterwide':{k:float(b['wal'][k])-float(a['wal'][k]) for k in ('wal_records','wal_fpi','wal_bytes','wal_buffers_full','wal_write','wal_sync','wal_write_time','wal_sync_time')},'database':{k:float(b['database'][k])-float(a['database'][k]) for k in a['database']}}
 else:info['pg_delta_error']='missing boundary snapshots'
 log=(p/'backend.log').read_text();info['pool_error_mentions']=log.count('QueuePool limit');info['tracebacks']=log.count('Traceback (most recent call last)')
 cpu=load(p/'cpu-samples.json');ticks=[x for x in load(p/'cpu-ticks.json') if start<=x['t']<=end];ff=cpu['frames'];cats=collections.Counter();routes=collections.Counter();gate=collections.Counter();leaf=collections.Counter();inclusive=collections.Counter();route_categories=collections.Counter()
 for t in ticks:
  routes[t['route']]+=1;gate[str(t['gate'])]+=1
  if t['stack']:
   f=ff[t['stack'][0]];cats[category(f)]+=1;route_categories[(t['route'],category(f))]+=1;leaf[f['file']+':'+f['name']]+=1
   for label in {ff[i]['file']+':'+ff[i]['name'] for i in t['stack']}:inclusive[label]+=1
 info['cpu_route_categories']=[dict(route=r,category=c,samples=n) for (r,c),n in route_categories.most_common()]
 info['cpu']={'enabled':cpu['enabled'],'errors':cpu['errors'],'stress_ticks':len(ticks),'excluded_ticks':len(load(p/'cpu-ticks.json'))-len(ticks),'routes':pct(routes),'held_gate':pct(gate),'leaf_categories':pct(cats),'top_leaf_functions':pct(leaf)[:35],'inclusive_functions':[dict(name=k,samples=v,percent_of_ticks=100*v/len(ticks)) for k,v in inclusive.most_common(40)] if ticks else []}
 if ticks:
  extra={r:len(ff)+i for i,r in enumerate(routes)};shared=ff+[dict(name='synthetic route: '+r) for r in routes]
  (p/'cpu.speedscope.json').write_text(json.dumps({'$schema':'https://www.speedscope.app/file-format-schema.json','shared':{'frames':shared},'profiles':[dict(type='sampled',name='stress SIGPROF 49Hz clipped to load-stop',unit='seconds',startValue=0,endValue=len(ticks)/49,samples=[[extra[t['route']]]+list(reversed(t['stack'])) for t in ticks],weights=[1/49]*len(ticks))]},indent=2)+'\n')
 native=p/'native-stress.speedscope.json'
 if native.exists():
  n=load(native);nc=collections.Counter();nl=collections.Counter();nf=n['shared']['frames'];total=0
  for pr in n['profiles']:
   for st in pr['samples']:
    total+=1
    if st:
     f=nf[st[-1]];nc[category(f)]+=1;nl[f.get('file','')+':'+f.get('name','')]+=1
  info['native']={'samples':total,'window':load(p/'native-window.json'),'log':(p/'native.log').read_text(),'leaf_categories':pct(nc),'top_leaf_functions':pct(nl)[:40]}
 result[variant]=info
(root/'analysis.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({v:{'abort':i['abort'],'cpu':i.get('cpu',{}),'resources':i.get('stress_resources',{}),'gate':i.get('gate_events',{})} for v,i in result.items()},ensure_ascii=False,indent=2))
