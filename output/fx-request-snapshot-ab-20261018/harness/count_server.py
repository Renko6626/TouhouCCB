import asyncio,collections,contextvars,json,os,re,time
from contextlib import asynccontextmanager
from pathlib import Path
from sqlalchemy import event
from app.core.database import engine
from app.services.credit.gates import SymbolGateSet,GATES
from local_server import app
root=Path(os.environ['LOCAL_RUN_DIRECTORY']);phase='startup';route=contextvars.ContextVar('route',default='background');counts=collections.Counter();gates=collections.defaultdict(list); dependency_counts=collections.Counter(); classifications=collections.Counter(); body_counts=collections.Counter(); lag=[]
def family(r):
    if r.startswith('background:'):return r
    if r=='background':return 'background_untagged'
    if r.startswith('POST ') or any(k in r for k in ('/borrow','/loan','/risk')):return 'business_quote_or_write'
    if r.startswith('GET /history/') or r.endswith('/chart'):return 'history'
    if r=='GET /api/v1/fx/pairs':return 'market_directory_retained'
    if '/my-trades' in r:return 'personal_trade_join_retained'
    if any(k in r for k in ('/user/trading-context','/user/trading-versions','/user/trading-prices','/stream/trading','/fx/stream/')) or (r.startswith('GET /api/v1/fx/pairs') and '/my-trades' not in r):return 'live_market_read'
    return 'other_http'
class Labels:
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        token=route.set(scope['method']+' '+re.sub(r'/\d+(?=/|$)','/:id',scope['path']))
        async def counted_send(message):
            if message['type']=='http.response.body':
                body_counts[(phase,route.get(),'frames')]+=1;body_counts[(phase,route.get(),'bytes')]+=len(message.get('body',b''))
            await send(message)
        try:return await self.app(scope,receive,counted_send)
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
    r=route.get();category='other'
    if r.startswith('GET /history/') or r.endswith('/chart'):category='history_http'
    elif kind:category='dependency_identities'
    elif text.startswith('select'):
        if 'for update' in text:category='write_authority_locked_select'
        elif 'from fx_pair' in text:
            category='fx_pool_version_lock' if text.startswith('select fx_pair.pool_version ') and 'for share' in text else 'fx_public_share_lock' if text.startswith('select fx_pair.id ') and 'for share' in text else 'fx_current_pair_select'
        elif 'from outcome' in text or 'from market ' in text:category='lmsr_market_input_or_dependency'
        elif 'fx_market_data_state' in text:category='fx_market_metadata_select'
        elif 'from fx_candle' in text:category='history_candle_select'
        elif 'from fx_trade' in text:category='trade_history_or_volume_select'
        elif any(t in text for t in ('from fx_wallet','from fx_short_position','from position','from "user"','from loan')):category='account_select'
        else:category='other_select'
    else:category=text.split(None,1)[0] if text else 'other'
    classifications[(phase,r,category)]+=1
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
    last=0;previous=time.monotonic()
    with (root/'gate-queue.jsonl').open('w') as out:
        while True:
            path=root/'phase.txt'
            if path.exists():phase=path.read_text().strip()
            now=time.monotonic();lag.append({'t':time.time(),'phase':phase,'lag_ms':max(0,now-previous-.05)*1000});previous=now
            if now-last>=.5:
                m=GATES.metrics();out.write(json.dumps({'t':time.time(),'phase':phase,'waiting':m.waiting,'holders':m.holders,'pool_checked_out':engine.pool.checkedout()})+'\n');out.flush();last=now
            await asyncio.sleep(.05)
def dist(values):
    s=sorted(values);n=len(s)
    return {'count':n,'mean_ms':sum(s)/n,'p50_ms':s[int((n-1)*.5)],'p95_ms':s[int((n-1)*.95)],'p99_ms':s[int((n-1)*.99)],'max_ms':s[-1]}
lifespan_original=app.router.lifespan_context
@asynccontextmanager
async def lifespan(app):
    async with lifespan_original(app):
        task=asyncio.create_task(track())
        try:yield
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
            (root/'sql-counts.json').write_text(json.dumps([{'phase':p,'route':r,'executions':n} for (p,r),n in counts.items()],indent=2))
            (root/'dependency-sql.json').write_text(json.dumps([{'phase':p,'route':r,'kind':k,'executions':n} for (p,r,k),n in dependency_counts.items()],indent=2))
            (root/'sql-classifications.json').write_text(json.dumps([{'phase':p,'route':r,'family':family(r),'category':c,'executions':n} for (p,r,c),n in classifications.items()],indent=2))
            (root/'body-counts.json').write_text(json.dumps([{'phase':p,'route':r,'kind':k,'count':n} for (p,r,k),n in body_counts.items()],indent=2))
            (root/'eventloop-lag.json').write_text(json.dumps(lag))
            (root/'gates.json').write_text(json.dumps([{'phase':p,'route':r,'kind':k,**dist(v)} for (p,r,k),v in gates.items()],indent=2))
app.router.lifespan_context=lifespan

# Component context is synthetic attribution, not complete asynchronous causality.
import functools
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
