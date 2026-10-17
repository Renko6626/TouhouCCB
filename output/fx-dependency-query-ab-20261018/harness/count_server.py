import asyncio,collections,contextvars,json,os,re,time
from contextlib import asynccontextmanager
from pathlib import Path
from sqlalchemy import event
from app.core.database import engine
from app.services.credit.gates import SymbolGateSet,GATES
from local_server import app
root=Path(os.environ['LOCAL_RUN_DIRECTORY']);phase='startup';route=contextvars.ContextVar('route',default='background');counts=collections.Counter();gates=collections.defaultdict(list); dependency_counts=collections.Counter()
class Labels:
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        token=route.set(scope['method']+' '+re.sub(r'/\d+(?=/|$)','/:id',scope['path']))
        try:return await self.app(scope,receive,send)
        finally:route.reset(token)
app.add_middleware(Labels)
@event.listens_for(engine.sync_engine,'before_cursor_execute')
def query(conn,cursor,statement,parameters,context,executemany):
    counts[(phase,route.get())]+=1
    text=' '.join(statement.lower().split())
    kind=None
    if 'union all' in text and 'fx_wallet.pair_id' in text and 'fx_short_position.pair_id' in text and 'outcome.market_id' in text:kind='dependency_union'
    elif text.startswith('select outcome.market_id ') and ' join position ' in text:kind='dependency_market'
    elif text.startswith('select fx_wallet.pair_id ') and ' where fx_wallet.user_id ' in text:kind='dependency_wallet'
    elif text.startswith('select fx_short_position.pair_id ') and ' where fx_short_position.user_id ' in text:kind='dependency_short'
    if kind:dependency_counts[(phase,route.get(),kind)]+=1
old_hold=SymbolGateSet.hold
@asynccontextmanager
async def hold(self,*a,**kw):
    p,r=phase,route.get();start=time.monotonic()
    async with old_hold(self,*a,**kw):
        gates[(p,r,'wait')].append((time.monotonic()-start)*1000);start=time.monotonic()
        try:yield
        finally:gates[(p,r,'held')].append((time.monotonic()-start)*1000)
SymbolGateSet.hold=hold
async def track():
    global phase
    last=0
    with (root/'gate-queue.jsonl').open('w') as out:
        while True:
            path=root/'phase.txt'
            if path.exists():phase=path.read_text().strip()
            now=time.monotonic()
            if now-last>=.5:
                m=GATES.metrics();out.write(json.dumps({'t':time.time(),'phase':phase,'waiting':m.waiting,'holders':m.holders,'pool_checked_out':engine.pool.checkedout()})+'\n');out.flush();last=now
            await asyncio.sleep(.05)
def dist(values):
    s=sorted(values);n=len(s)
    return {'count':n,'mean_ms':sum(s)/n,'p50_ms':s[int((n-1)*.5)],'p95_ms':s[int((n-1)*.95)],'p99_ms':s[int((n-1)*.99)],'max_ms':s[-1]}
original=app.router.lifespan_context
@asynccontextmanager
async def lifespan(app):
    async with original(app):
        task=asyncio.create_task(track())
        try:yield
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
            (root/'sql-counts.json').write_text(json.dumps([{'phase':p,'route':r,'executions':n} for (p,r),n in counts.items()],indent=2))
            (root/'dependency-sql.json').write_text(json.dumps([{'phase':p,'route':r,'kind':k,'executions':n} for (p,r,k),n in dependency_counts.items()],indent=2))
            (root/'gates.json').write_text(json.dumps([{'phase':p,'route':r,'kind':k,**dist(v)} for (p,r,k),v in gates.items()],indent=2))
app.router.lifespan_context=lifespan
