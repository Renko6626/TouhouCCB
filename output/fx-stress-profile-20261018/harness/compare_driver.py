import os,time,json,random,asyncio
from pathlib import Path
import fx_http
root=Path(os.environ['LOCAL_RUN_DIRECTORY']);original=fx_http.Metrics.__setattr__;abort=fx_http.Metrics.abort;stage=fx_http.Scenario.stage if hasattr(fx_http,'Scenario') else None
# Stage class is discovered by its actual method, without editing production or workload logic.
for obj in vars(fx_http).values():
 if isinstance(obj,type) and hasattr(obj,'stage') and hasattr(obj,'trade'):scenario=obj;break
original_stage=scenario.stage
async def measured_stage(self,name,*a,**kw):
 if name=='stress':(root/'stress-started.json').write_text(json.dumps({'t':time.time()}))
 try:return await original_stage(self,name,*a,**kw)
 finally:
  if name=='stress':(root/'stress-ended.json').write_text(json.dumps({'t':time.time()}))
scenario.stage=measured_stage
def setter(self,name,value):
 original(self,name,value)
 if name=='phase':
  (root/'phase.txt').write_text(value)
  with (root/'phases.jsonl').open('a') as f:f.write(json.dumps({'phase':value,'t':time.time()})+'\n')
def stopped(self,reason):
 if not self.stop.is_set():(root/'protection.json').write_text(json.dumps({'t':time.time(),'phase':self.phase,'reason':reason}))
 return abort(self,reason)
fx_http.Metrics.__setattr__=setter;fx_http.Metrics.abort=stopped
random.seed(20261017)
try:fx_http.cli()
finally:(root/'driver-ended').write_text(str(time.time()))
