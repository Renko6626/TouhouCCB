import asyncio,collections,contextvars,functools,json,os,re,signal,sys,time
from contextlib import asynccontextmanager
from pathlib import Path
from sqlalchemy import event
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession
from app.main import app
from app.core.database import engine
from app.services.credit.gates import SymbolGateSet,GATES
root=Path(os.environ['LOCAL_RUN_DIRECTORY']);profile=os.environ['CPU_PROFILE']=='1';phase='startup';observing=False;heavy=False;loop=None
route=contextvars.ContextVar('route',default='background');cells={};metrics=collections.defaultdict(list);sql=collections.defaultdict(lambda:[0,0.]);counts=collections.Counter();bodies=collections.Counter();gate_rows=[];samples=collections.Counter();frames={};frame_data=[];sample_errors=0;sampling=False;peak=0;windows=[];ticks=[];timed_events=[]
def dump(name,data):(root/name).write_text(json.dumps(data,default=str,indent=2)+'\n')
def measure(name,t,p=None,r=None):
 metrics[(p or phase,r or route.get(),name)].append(t*1000)
 if (p or phase)=='stress':
  ended=time.time();timed_events.append(dict(start=ended-t,end=ended,name=name,route=r or route.get(),ms=t*1000))
def task():return asyncio.tasks._current_tasks.get(loop)
def cell():return cells.get(task())
(root/'server.pid').write_text(str(os.getpid()))
if profile:
 import ctypes
 libc=ctypes.CDLL(None,use_errno=True);libc.prctl.argtypes=[ctypes.c_int,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_ulong]
 dump('ptrace-status.json',{'status':libc.prctl(0x59616d61,ctypes.c_ulong(-1).value,0,0,0),'errno':ctypes.get_errno()})
class Labels:
 def __init__(self,app):self.app=app
 async def __call__(self,scope,receive,send):
  if scope['type']!='http':return await self.app(scope,receive,send)
  token=route.set(scope['method']+' '+re.sub(r'/\d+(?=/|$)','/:id',scope['path']))
  async def measured_send(message):
   if observing and message['type']=='http.response.body':
    body=message.get('body',b'');bodies[(route.get(),'frames')]+=1;bodies[(route.get(),'bytes')]+=len(body)
   await send(message)
  try:return await self.app(scope,receive,measured_send)
  finally:route.reset(token)
app.add_middleware(Labels)
@event.listens_for(engine.sync_engine,'checkout')
def checkout(*a):
 global peak
 peak=max(peak,engine.pool.checkedout())
@event.listens_for(engine.sync_engine,'before_cursor_execute')
def pre(conn,cursor,statement,params,ctx,many):
 counts[(phase,route.get())]+=1
 if heavy:ctx._profile=(time.perf_counter(),phase,route.get(),cell(),statement.split(None,1)[0])
@event.listens_for(engine.sync_engine,'after_cursor_execute')
def post(conn,cursor,statement,params,ctx,many):
 data=getattr(ctx,'_profile',None)
 if data is None:return
 start,p,r,c,kind=data;elapsed=time.perf_counter()-start;s=sql[(p,r,kind)];s[0]+=1;s[1]+=elapsed*1000
 if c is not None:c['sql_s']+=elapsed;c['sql_count']+=1
@event.listens_for(engine.sync_engine,'commit')
def commit_begin(conn):
 if heavy:
  c=cell()
  if c is not None:c['commit_begin']=time.perf_counter()
@event.listens_for(Session,'after_commit')
def committed(session):
 c=cell()
 if c is not None and 'commit_begin' in c:c['commit_s']+=time.perf_counter()-c.pop('commit_begin')
@event.listens_for(Session,'before_flush')
def flushing(session,ctx,instances):
 if heavy:session.info['_profile_flush']=(time.perf_counter(),phase,route.get(),cell())
@event.listens_for(Session,'after_flush_postexec')
def flushed(session,ctx):
 data=session.info.pop('_profile_flush',None)
 if data:
  start,p,r,c=data;elapsed=time.perf_counter()-start;measure('flush_inclusive',elapsed,p,r)
  if c is not None:c['flush_s']+=elapsed
old_commit=AsyncSession.commit
async def commit(self,*a,**kw):
 if not heavy:return await old_commit(self,*a,**kw)
 start=time.perf_counter();p,r=phase,route.get()
 try:return await old_commit(self,*a,**kw)
 finally:measure('commit_including_flush',time.perf_counter()-start,p,r)
AsyncSession.commit=commit
old_get=engine.pool._do_get
def get():
 if not heavy:return old_get()
 start=time.perf_counter();p,r=phase,route.get()
 try:return old_get()
 finally:measure('pool_do_get_including_creation_scheduling',time.perf_counter()-start,p,r)
engine.pool._do_get=get
old_hold=SymbolGateSet.hold
@asynccontextmanager
async def hold(self,*a,**kw):
 p,r=phase,route.get();start=time.perf_counter();requested=time.time();initial=observing
 async with old_hold(self,*a,**kw):
  wait=time.perf_counter()-start;measure('gate_wait',wait,p,r);start=time.perf_counter();acquired=time.time()
  c=dict(sql_s=0.,sql_count=0,commit_s=0.,flush_s=0.);t=task();cells[t]=c;timed=heavy
  try:yield
  finally:
   elapsed=time.perf_counter()-start;cells.pop(t,None);measure('gate_held',elapsed,p,r)
   if initial or observing:
    gate_rows.append(dict(requested=requested,acquired=acquired,released=time.time(),route=r,phase_at_request=p,wait_ms=wait*1000,held_ms=elapsed*1000,detailed=timed,**c))
SymbolGateSet.hold=hold
# Async elapsed timings only switch on inside the stress measurement window.
from app.services.credit import risk
from app.services.fx import net_trading
for module,name in ((risk,'discover_dependencies'),(risk,'check_new_risk'),(net_trading,'_simulate')):
 original=getattr(module,name)
 def timing(original=original,name=name):
  @functools.wraps(original)
  async def wrapper(*a,**kw):
   if not heavy:return await original(*a,**kw)
   start=time.perf_counter();p,r=phase,route.get()
   try:return await original(*a,**kw)
   finally:measure(name,time.perf_counter()-start,p,r)
  return wrapper
 wrapper=timing()
 for mod in list(sys.modules.values()):
  if mod and getattr(mod,'__name__','').startswith('app.'):
   for attr,value in list(vars(mod).items()):
    if value is original:setattr(mod,attr,wrapper)
# The same synthetic background labels are installed for both rounds before startup.
from app.services.trading_prices import PricingPublisher
from app.services.fx.publisher import FxPublisher
from app.services.fx.market_state import FxMarketDataService
for cls,name,label in ((PricingPublisher,'run','pricing_push'),(FxPublisher,'_run','public_fx_push'),(FxMarketDataService,'_run','market_data')):
 original=getattr(cls,name)
 def component(original=original,label=label):
  @functools.wraps(original)
  async def wrapper(*a,**kw):
   token=route.set('background:'+label)
   try:return await original(*a,**kw)
   finally:route.reset(token)
  return wrapper
 setattr(cls,name,component())
def sample(signum,frame):
 global sampling,sample_errors
 if sampling or not observing:return
 sampling=True
 try:
  stack=[]
  while frame is not None and len(stack)<72:
   code=frame.f_code;i=frames.get(code)
   if i is None:i=len(frame_data);frames[code]=i;frame_data.append({'file':code.co_filename,'name':code.co_name,'line':code.co_firstlineno})
   stack.append(i);frame=frame.f_back
  r,g=route.get(),task() in cells;samples[(r,g,tuple(stack))]+=1;ticks.append(dict(t=time.time(),route=r,gate=g,stack=stack))
 except Exception:sample_errors+=1
 finally:sampling=False
async def tick():
 global phase,observing,heavy,peak
 prev=time.perf_counter();cpu=time.process_time();last_resource=prev
 with (root/'resources.jsonl').open('w') as out:
  while True:
   await asyncio.sleep(.05);now=time.perf_counter();measure('event_loop_lag',max(0.,now-prev-.05),r='background');prev=now
   path=root/'phase.txt'
   if path.exists():phase=path.read_text().strip()
   desired=phase=='stress' and not (root/'protection.json').exists() and not (root/'stress-ended.json').exists()
   if desired!=observing:
    observing=desired;heavy=profile and desired;windows.append({'t':time.time(),'enabled':desired,'phase':phase})
    if profile:signal.setitimer(signal.ITIMER_PROF,1/49 if desired else 0,1/49 if desired else 0)
    dump('instrument-windows.json',windows)
   if now-last_resource>=.5:
    c=time.process_time();m=GATES.metrics();out.write(json.dumps(dict(t=time.time(),phase=phase,observing=observing,elapsed_s=now-last_resource,cpu_s=c-cpu,pool_checked_out=engine.pool.checkedout(),pool_peak_since_sample=peak,gate_waiting=m.waiting,gate_holders=m.holders))+'\n');out.flush();peak=engine.pool.checkedout();last_resource=now;cpu=c
original=app.router.lifespan_context
@asynccontextmanager
async def lifespan(app):
 global loop,observing,heavy
 loop=asyncio.get_running_loop()
 async with original(app):
  if profile:signal.signal(signal.SIGPROF,sample)
  worker=asyncio.create_task(tick())
  try:yield
  finally:
   observing=heavy=False
   if profile:signal.setitimer(signal.ITIMER_PROF,0)
   worker.cancel();results=await asyncio.gather(worker,return_exceptions=True)
   dump('monitor-errors.json',[repr(x) for x in results if isinstance(x,Exception)])
   dump('timings.json',[dict(phase=p,route=r,name=n,values_ms=v) for (p,r,n),v in metrics.items()])
   dump('sql.json',[dict(phase=p,route=r,kind=k,count=v[0],total_ms=v[1]) for (p,r,k),v in sql.items()])
   dump('sql-counts.json',[dict(phase=p,route=r,count=n) for (p,r),n in counts.items()]);dump('gate-events.json',gate_rows)
   dump('timed-events.json',timed_events);dump('cpu-ticks.json',ticks)
   dump('body-counts.json',[dict(route=r,kind=k,count=n) for (r,k),n in bodies.items()])
   dump('cpu-samples.json',dict(rate_cpu_hz=49,enabled=profile,errors=sample_errors,frames=frame_data,samples=[dict(route=r,gate=g,stack=list(st),count=n) for (r,g,st),n in samples.items()]))
app.router.lifespan_context=lifespan
