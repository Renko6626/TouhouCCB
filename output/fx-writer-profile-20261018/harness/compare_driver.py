import os,time,json,random,asyncio
from pathlib import Path
import fx_http
root=Path(os.environ['LOCAL_RUN_DIRECTORY']);original=fx_http.Metrics.__setattr__;abort=fx_http.Metrics.abort;stage=fx_http.Runner.stage
async def readheavy(self,users):
 name='readheavy';(root/'readheavy-started.json').write_text(json.dumps({'t':time.time()}));self.m.phase=name;self.m.phase_started[name]=time.monotonic();self.m.recent.clear();start=time.monotonic();deadline=start+60;sse={k:dict(self.m.sse_counts[k]) for k in ('public','private')};sem=asyncio.Semaphore(10);self.m.stage_metadata[name]={'planned_fixed_reads':1200,'planned_low_frequency_cycles':30,'read_rounds':3,'read_users':100,'read_concurrency_cap':10}
 async def sleep_until(at):await self.pause(max(0,at-time.monotonic()))
 async def reads(user,i):
  for round in range(3):
   await sleep_until(start+round*20+i*.15)
   if self.m.stop.is_set():return
   async with sem:
    for label,path,params in [('fixed_context','/api/v1/user/trading-context',self.group),('fixed_versions','/api/v1/user/trading-versions',self.group),('fixed_prices','/api/v1/user/trading-prices',{**self.group,'key':f'fx:{self.pid}'}),('fixed_snapshot',f'/api/v1/fx/pairs/{self.pid}/snapshot',None)]:
     if self.m.stop.is_set():return
     await self.request(user,label,'GET',path,params=params)
 async def writes():
  for i in range(30):
   await sleep_until(start+i*2)
   if self.m.stop.is_set():return
   self.m.trade_targets[name]+=1
   await self.trade(users[i])
 jobs=[asyncio.create_task(reads(u,i)) for i,u in enumerate(users)]+[asyncio.create_task(writes())]
 try:
  while time.monotonic()<deadline and not self.m.stop.is_set():await asyncio.sleep(.1)
  await asyncio.gather(*jobs)
 finally:
  for job in jobs:job.cancel()
  await asyncio.gather(*jobs,return_exceptions=True)
  (root/'readheavy-ended.json').write_text(json.dumps({'t':time.time()}))
  elapsed=time.monotonic()-start;success=self.m.counts[(name,'trade')]['ok'];requests=sum(sum(c.values()) for (p,n),c in self.m.counts.items() if p==name and n!='cycle' and not n.startswith('sse_'))
  self.m.stages.append(dict(name=name,duration_s=round(elapsed,3),target_users=100,active_users=30,attempted_trade_cycles=self.m.trade_targets[name],successful_trades=success,successful_trades_per_s=round(success/elapsed,3),completed_requests=requests,completed_requests_per_s=round(requests/elapsed,3),public_connected_at_end=len(self.m.connected['public']),private_connected_at_end=len(self.m.connected['private']),planned_fixed_reads=1200,planned_trade_cycles=30,sse_received={k:{key:self.m.sse_counts[k][key]-sse[k].get(key,0) for key in ('frames','payload_bytes')} for k in sse}))
async def wrapped_stage(self,name,users,*a,**kw):
 if name=='readonly' and not getattr(self,'_readheavy_done',False):self._readheavy_done=True;await readheavy(self,users)
 if self.m.stop.is_set():return
 (root/(name+'-started.json')).write_text(json.dumps({'t':time.time()}))
 try:return await stage(self,name,users,*a,**kw)
 finally:(root/(name+'-ended.json')).write_text(json.dumps({'t':time.time()}))
def setter(self,name,value):
 original(self,name,value)
 if name=='phase':
  (root/'phase.txt').write_text(value)
  with (root/'phases.jsonl').open('a') as f:f.write(json.dumps({'phase':value,'t':time.time()})+'\n')
def stopped(self,reason):
 if not self.stop.is_set():(root/'protection.json').write_text(json.dumps({'t':time.time(),'phase':self.phase,'reason':reason}))
 return abort(self,reason)
fx_http.Metrics.__setattr__=setter;fx_http.Metrics.abort=stopped;fx_http.Runner.stage=wrapped_stage
random.seed(20261017)
try:fx_http.cli()
finally:(root/'driver-ended').write_text(str(time.time()))
