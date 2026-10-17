import asyncio,collections,json,os,time
from pathlib import Path
import httpx
root=Path(os.environ['LOCAL_RUN_DIRECTORY'])
async def main():
    creds=json.loads((root/'private/tokens.json').read_text());users=creds['users'][:100];pid=creds['pair_id'];out={}
    async with httpx.AsyncClient(base_url='http://127.0.0.1:18543',timeout=30,limits=httpx.Limits(max_connections=10,max_keepalive_connections=10)) as client:
        for endpoint in ('versions','prices'):
            for label,rounds in (('warmup',1),('measured',10)):
                phase=f'deterministic_{endpoint}_{label}';(root/'phase.txt').write_text(phase);await asyncio.sleep(.15)
                values=[];status=collections.Counter();sem=asyncio.Semaphore(10)
                async def request(i):
                    async with sem:
                        user=users[i%100];start=time.perf_counter()
                        params={'current_product':'fx','current_id':pid}
                        if endpoint=='prices':params['key']=f'fx:{pid}'
                        try:
                            response=await client.get('/api/v1/user/trading-'+endpoint,params=params,headers={'Authorization':'Bearer '+user['token']});status[str(response.status_code)]+=1
                        except Exception as e:status[type(e).__name__]+=1
                        values.append((time.perf_counter()-start)*1000)
                start=time.perf_counter();await asyncio.gather(*(request(i) for i in range(100*rounds)));elapsed=time.perf_counter()-start
                values.sort();n=len(values)
                out[phase]={'requests':n,'concurrency':10,'duration_s':elapsed,'requests_per_s':n/elapsed,'statuses':dict(status),'latency_ms':{'mean':sum(values)/n,'p50':values[int((n-1)*.5)],'p95':values[int((n-1)*.95)],'p99':values[int((n-1)*.99)]}}
    (root/'deterministic.json').write_text(json.dumps(out,indent=2)+'\n')
asyncio.run(main())
