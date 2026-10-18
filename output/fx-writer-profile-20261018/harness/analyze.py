import collections,json,statistics
from pathlib import Path
out=Path('/data/sunyunbo/www/TouhouCCB/output/fx-writer-profile-20261018')
data=out/'profile'
def load(name):return json.loads((data/name).read_text())
def dist(v):
    s=sorted(v);n=len(s)
    return {'count':n,'total_ms':sum(s),'mean_ms':sum(s)/n,'p50_ms':s[int((n-1)*.5)],'p95_ms':s[int((n-1)*.95)],'p99_ms':s[int((n-1)*.99)],'max_ms':s[-1]} if n else {}
cpu=load('cpu-samples.json');frames=cpu['frames'];ticks=cpu['ticks']
def frame_name(i):
    f=frames[i];return f['file']+':'+str(f['line'])+' '+f['name']
stats={}
for phase in ('active','stress'):
    ts=[t for t in ticks if t['phase']==phase];routes=collections.Counter();spans=collections.Counter();leaf=collections.Counter();inclusive=collections.Counter()
    for t in ts:
        routes[t['route']]+=1;spans[(t['route'],t['span'])]+=1
        if t['stack']:leaf[frame_name(t['stack'][0])]+=1
        inclusive.update(set(frame_name(i) for i in t['stack']))
    stats[phase]={'samples':len(ts),'routes':routes.most_common(),'spans':[{'route':r,'span':s,'samples':n} for (r,s),n in spans.most_common()],'leaf':leaf.most_common(80),'inclusive':inclusive.most_common(80)}
events=load('profile-events.json');grouped=collections.defaultdict(list)
for e in events:grouped[(e['phase'],e['route'],e['command'],e['name'],e['parent'],e['outcome'])].append(e['ms'])
event_stats=[dict(phase=p,route=r,command=c,name=n,parent=a,outcome=o,**dist(v)) for (p,r,c,n,a,o),v in grouped.items()]
cmds=load('profile-commands.json');command_stats=[]
for key in sorted(set((c['phase'],c['command'],c['outcome']) for c in cmds)):
    cs=[c for c in cmds if (c['phase'],c['command'],c['outcome'])==key]
    command_stats.append({'phase':key[0],'command':key[1],'outcome':key[2],'elapsed':dist([c['ms'] for c in cs]),'sql_cursor_elapsed':dist([c['sql_ms'] for c in cs]),'sql_count':dist([c['sql_count'] for c in cs])})
sql=load('profile-sql.json')
native={}
p=data/'native-stress.speedscope.json'
if p.exists():
    d=json.loads(p.read_text());nf=d['shared']['frames'];leaves=collections.Counter();inc=collections.Counter();total=0
    for profile in d['profiles']:
        for sample,w in zip(profile['samples'],profile.get('weights',[1]*len(profile['samples']))):
            total+=w
            def key(i):
                f=nf[i];return f.get('file','')+':'+str(f.get('line',''))+' '+f['name']
            if sample:leaves[key(sample[-1])]+=w
            inc.update({k:w for k in set(key(i) for i in sample)})
    native={'weight':total,'leaf':leaves.most_common(80),'inclusive':inc.most_common(80)}
result={'cpu':stats,'sample_errors':cpu['errors'],'windows':cpu['windows'],'event_stats':event_stats,'commands':command_stats,'sql':sorted(sql,key=lambda x:x['total_ms'],reverse=True),'native':native}
(out/'analysis.json').write_text(json.dumps(result,indent=2))
for p,s in stats.items():
    print(p,s['samples'],'routes',s['routes'][:12]);print('leaf',s['leaf'][:12])
print('commands',command_stats)
print('native',native.get('leaf',[])[:15])
