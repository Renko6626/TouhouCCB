"""Experiment-only profiling of committed code. No business changes."""
import asyncio, collections, contextvars, ctypes, functools, inspect, json, os, signal, sys, time
from contextlib import asynccontextmanager
from pathlib import Path
import count_server as base
from count_server import app, engine, route
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

root=Path(os.environ['LOCAL_RUN_DIRECTORY'])
enabled=False; loop=None; sampling=False; errors=0
current=contextvars.ContextVar('profile_command',default=None)
span=contextvars.ContextVar('profile_span',default='outside_command')
events=[]; commands=[]; ticks=[]; frames=[]; frame_ids={}; windows=[]
sql=collections.defaultdict(lambda:[0,0.0])
(root/'server.pid').write_text(str(os.getpid()))
libc=ctypes.CDLL(None,use_errno=True)
libc.prctl.argtypes=[ctypes.c_int,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_ulong]
ptrace=libc.prctl(0x59616d61,ctypes.c_ulong(-1).value,0,0,0)

def active(): return enabled
def dump(name,value): (root/name).write_text(json.dumps(value,default=str,indent=2)+'\n')
def replace_refs(original,replacement):
    for module in list(sys.modules.values()):
        if module and getattr(module,'__name__','').startswith('app.'):
            for key,value in list(vars(module).items()):
                if value is original:setattr(module,key,replacement)

def instrument(module,name,command=False):
    original=getattr(module,name)
    assert inspect.iscoroutinefunction(original),(module,name)
    label=module.__name__+'.'+name
    @functools.wraps(original)
    async def wrapped(*args,**kwargs):
        if not active(): return await original(*args,**kwargs)
        record=None; token=None
        if command:
            cmd=args[1]
            record={'command':type(cmd).__name__,'phase':base.phase,'start':time.time(),'sql_ms':0.,'sql_count':0}
            token=current.set(record)
        start=time.perf_counter(); p=base.phase; parent=span.get(); st=span.set(label);outcome='ok'
        try:return await original(*args,**kwargs)
        except BaseException as exc:outcome=type(exc).__name__;raise
        finally:
            elapsed=(time.perf_counter()-start)*1000
            events.append({'phase':p,'route':route.get(),'name':label,'parent':parent,'ms':elapsed,'outcome':outcome,'command':(current.get() or {}).get('command')})
            span.reset(st)
            if record is not None:
                record.update(ms=elapsed,end=time.time(),outcome=outcome);commands.append(record);current.reset(token)
    replace_refs(original,wrapped)

from app.services.fx import writer_ops,net_trading,shorts,trading
from app.services.credit import risk,valuation
from app.services import loan_service
for module,names in (
    (writer_ops,['execute_command','_execute_in_session']),
    (net_trading,['execute_net_trade_in_session','_simulate','quote_net_trade']),
    (risk,['discover_dependencies','check_new_risk','_prepare_post','_current_rates','_collateral_value']),
    (valuation,['_account_valuations']),
    (shorts,['execute_short_cover_in_session']),
    (trading,['_write_spot_ledger']),
    (loan_service,['increase_debt_locked']),
):
    for name in names:instrument(module,name,command=module is writer_ops and name=='execute_command')

for name in ('flush','commit','execute'):
    original=getattr(AsyncSession,name)
    def build(original=original,name=name):
        @functools.wraps(original)
        async def wrapper(self,*args,**kwargs):
            if not active():return await original(self,*args,**kwargs)
            start=time.perf_counter();p=base.phase;r=route.get();parent=span.get();outcome='ok'
            try:return await original(self,*args,**kwargs)
            except BaseException as exc:outcome=type(exc).__name__;raise
            finally:events.append({'phase':p,'route':r,'name':'AsyncSession.'+name,'parent':parent,'ms':(time.perf_counter()-start)*1000,'outcome':outcome,'command':(current.get() or {}).get('command')})
        return wrapper
    setattr(AsyncSession,name,build())

@event.listens_for(engine.sync_engine,'before_cursor_execute')
def before(conn,cursor,statement,params,ctx,many):
    if active():ctx._detail=(time.perf_counter(),base.phase,route.get(),span.get(),current.get(),statement)
@event.listens_for(engine.sync_engine,'after_cursor_execute')
def after(conn,cursor,statement,params,ctx,many):
    d=getattr(ctx,'_detail',None)
    if d is None:return
    start,p,r,s,c,query=d;ms=(time.perf_counter()-start)*1000
    key=(p,r,s,' '.join(query.split()));row=sql[key];row[0]+=1;row[1]+=ms
    if c is not None:c['sql_ms']+=ms;c['sql_count']+=1

def sample(signum,frame):
    global sampling,errors
    if sampling or not enabled:return
    sampling=True
    try:
        stack=[]
        while frame is not None and len(stack)<80:
            code=frame.f_code;i=frame_ids.get(code)
            if i is None:
                i=len(frames);frame_ids[code]=i;frames.append({'file':code.co_filename,'name':code.co_name,'line':code.co_firstlineno})
            stack.append(i);frame=frame.f_back
        ticks.append({'t':time.time(),'phase':base.phase,'route':route.get(),'span':span.get(),'command':(current.get() or {}).get('command'),'stack':stack})
    except Exception:errors+=1
    finally:sampling=False

async def track():
    global enabled
    while True:
        desired=base.phase in ('active','stress') and not (root/'protection.json').exists()
        if desired!=enabled:
            enabled=desired;windows.append({'t':time.time(),'enabled':enabled,'phase':base.phase})
            signal.setitimer(signal.ITIMER_PROF,1/49 if desired else 0,1/49 if desired else 0)
        await asyncio.sleep(.05)

original=app.router.lifespan_context
@asynccontextmanager
async def lifespan(app):
    global enabled,loop
    loop=asyncio.get_running_loop();signal.signal(signal.SIGPROF,sample)
    async with original(app):
        task=asyncio.create_task(track())
        try:yield
        finally:
            enabled=False;signal.setitimer(signal.ITIMER_PROF,0)
            task.cancel();await asyncio.gather(task,return_exceptions=True)
            dump('profile-events.json',events);dump('profile-commands.json',commands)
            dump('profile-sql.json',[{'phase':p,'route':r,'span':s,'statement':q,'count':v[0],'total_ms':v[1]} for (p,r,s,q),v in sql.items()])
            dump('cpu-samples.json',{'hz':49,'errors':errors,'frames':frames,'ticks':ticks,'ptrace_status':ptrace,'windows':windows})
app.router.lifespan_context=lifespan
