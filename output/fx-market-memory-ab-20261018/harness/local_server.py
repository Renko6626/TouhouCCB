import asyncio,json,os,time
from contextlib import asynccontextmanager
from pathlib import Path
import asyncpg
from sqlalchemy import event
from app.main import app
from app.core.database import engine

original=app.router.lifespan_context
peak=0
@event.listens_for(engine.sync_engine,'checkout')
def checkout(*args):
    global peak
    peak=max(peak,engine.pool.checkedout())

async def sample():
    global peak
    conn=await asyncpg.connect(database=os.environ['LOCAL_DB_NAME'],user='sunyunbo',host='/tmp/sunyunbo/fx-pool-incident-20261017/socket',port=55489)
    last_cpu=time.process_time();last_time=time.monotonic()
    try:
        with open(Path(os.environ['LOCAL_RUN_DIRECTORY'])/'resources.jsonl','w') as out:
            while True:
                rows=await conn.fetch("SELECT state, wait_event_type, count(*) AS n FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() GROUP BY state,wait_event_type")
                out.write(json.dumps({'t':time.time(),'pool_checked_out':engine.pool.checkedout(),'pool_peak_since_sample':peak,'pool_size':engine.pool.size(),'pool_overflow':engine.pool.overflow(),'app_cpu_percent':round((time.process_time()-last_cpu)/max(.001,time.monotonic()-last_time)*100,2),'app_rss_mb':round(int(Path('/proc/self/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')/1024**2,2),'db_states':[dict(r) for r in rows]})+'\n');out.flush();peak=engine.pool.checkedout();last_cpu=time.process_time();last_time=time.monotonic();await asyncio.sleep(1)
    finally:await conn.close()

@asynccontextmanager
async def lifespan(app):
    async with original(app):
        task=asyncio.create_task(sample())
        try:yield
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
app.router.lifespan_context=lifespan
