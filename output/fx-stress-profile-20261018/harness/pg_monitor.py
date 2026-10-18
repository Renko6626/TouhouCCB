import asyncio,json,os,time
from pathlib import Path
import asyncpg
root=Path(os.environ['LOCAL_RUN_DIRECTORY'])
def cpu(pid):
 try:
  parts=Path('/proc/'+str(pid)+'/stat').read_text().rsplit(')',1)[1].split();return (int(parts[11])+int(parts[12]))/os.sysconf('SC_CLK_TCK')
 except (FileNotFoundError,ProcessLookupError):return None
async def main():
 conn=await asyncpg.connect(database=os.environ['LOCAL_DB_NAME'],user='sunyunbo',host='/tmp/sunyunbo/fx-pool-incident-20261017/socket',port=55489);errors=[];started=False;ended=False;previous={}
 async def snapshot(label):
  rows=await conn.fetch("SELECT queryid::text,query,calls,total_exec_time,total_plan_time,rows,shared_blks_hit,shared_blks_read,blk_read_time,blk_write_time,wal_bytes::float8 FROM pg_stat_statements WHERE dbid=(SELECT oid FROM pg_database WHERE datname=current_database()) AND userid=(SELECT usesysid FROM pg_user WHERE usename=current_user) AND query NOT ILIKE '%pg_stat_%' AND query NOT ILIKE '%pg_blocking_pids%'")
  wal=await conn.fetchrow('SELECT * FROM pg_stat_wal');db=await conn.fetchrow('SELECT blks_read,blks_hit,blk_read_time,blk_write_time,xact_commit,xact_rollback,temp_bytes FROM pg_stat_database WHERE datname=current_database()')
  with (root/'pg-snapshots.jsonl').open('a') as out:out.write(json.dumps(dict(t=time.time(),label=label,queries=[dict(r) for r in rows],wal=dict(wal),database=dict(db)),default=str)+'\n')
 try:
  settings={k:await conn.fetchval('SHOW '+k) for k in ('fsync','synchronous_commit','track_io_timing','track_wal_io_timing','shared_preload_libraries','track_activity_query_size')};(root/'pg-settings.json').write_text(json.dumps(settings,indent=2)+'\n')
  with (root/'pg-activity.jsonl').open('w') as out:
   last=0
   while not (root/'monitor-stop').exists():
    phasepath=root/'phase.txt';phase=phasepath.read_text().strip() if phasepath.exists() else 'startup'
    if phase=='stress' and not started:await snapshot('stress_entry');started=True
    if started and not ended and ((root/'protection.json').exists() or (root/'stress-ended.json').exists()):await snapshot('stress_protection_or_end');ended=True
    if time.monotonic()-last>=.5:
     states=await conn.fetch("SELECT pid,state,wait_event_type,wait_event,query_id::text,pg_blocking_pids(pid) AS blocking_pids FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()")
     total=0.;pids={}
     for row in states:
      pid=row['pid'];v=cpu(pid)
      if v is not None:total+=max(0.,v-previous.get(pid,v));previous[pid]=v;pids[str(pid)]=v
     now=time.monotonic();out.write(json.dumps(dict(t=time.time(),phase=phase,stress_started=started,stress_ended=ended,elapsed_s=now-last if last else 0,pg_cpu_s=total,backend_cumulative_cpu_s=pids,activity=[dict(r) for r in states]))+'\n');out.flush();last=now
    await asyncio.sleep(.05)
  if started:await snapshot('monitor_exit')
 except Exception as exc:errors.append(repr(exc));raise
 finally:
  (root/'pg-monitor-errors.json').write_text(json.dumps(errors)+'\n');await conn.close()
asyncio.run(main())
