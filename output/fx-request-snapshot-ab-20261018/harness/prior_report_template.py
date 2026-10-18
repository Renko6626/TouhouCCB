import json,collections
from pathlib import Path
root=Path('/data/sunyunbo/www/TouhouCCB/output/fx-request-snapshot-ab-20261018');data=json.loads((root/'analysis.json').read_text());fmt=lambda x:f'{x:.2f}' if isinstance(x,(int,float)) else '—'
def market(s):return sum(x['executions'] for x in s['sql_classifications'] if x['category']=='fx_current_pair_select' and x['family'] in ('live_market_read','background:pricing_push','background:public_fx_push'))
def guard(s):return sum(x['executions'] for x in s['sql_classifications'] if x['category'] in ('fx_pool_version_lock','fx_public_share_lock'))
def held(s,route,kind):return next((x for x in s['gate_by_route'] if x['route']==route and x['kind']==kind),{})
lines=['# 市场输入内存读取：本机A/B','','固定readheavy：目标行情Pair输入1367→0，总SQL8925→7570（约15.2%下降），应用CPU均值39.75%→36.78%（约2.97个百分点）。normal/active成交吞吐近似；stress两版都触发原P99保护并未运行burst，after8.895成交/s低于before9.537。本轮不能认定交易容量改善，也不能认定所有读尾延迟改善；单轮、固定执行顺序及实际请求组成差异，不足以把所有变化确定归因于迁移。','','低频固定versions P99 65.74→87.95ms，active context_after_trade 385→947ms、周期versions356→936ms；stress context_after_trade384→7135ms、周期versions400→6947ms。after context共享GATE等待P99约6920ms，表明高压读排队明显加重；不能用SQL减少掩盖读体验回退。公共SSE全轮event-age P99 75.77→42.86ms，私人147.36→160.49ms，亦非所有推送均改善。下一阶段决策点：已确认的账户覆盖sharedGATE在高写入下出现排队，当前证据不能证明SQL/CPU收益足以替代读尾损失；尚未部署，本轮不擅自修改锁或一致性边界。','','固定before `330f4e683ec5564dea6ea59983351c3c8e128d9d`、after `330f4e683ec5564dea6ea59983351c3c8e128d9d`，均由git archive提取；before已包含融资优化与依赖UNION ALL，after含本次市场提交快照/读取迁移。全backend hash与实际diff见before-source.json、after-source.json、source.diff。性能脚本未改业务源或提交，没有部署；正确性验证由主agent另记validation.md，本harness没有重复PG测试。','','## 方法与边界','','本机PG14 Unix socket55489、不连接生产；两版分别新建fx_market_memory_{before,after}_r1_test，单Uvicorn worker18543、池10+20，同seed20261017，100用户/200SSE（公开私人各100）、角色现金/融资/空头60/20/20。主agent拥有PG生命周期，本harness结束只关闭自己启动的HTTP/monitor并删除tokens，不关闭cluster。','','same轻量SQL计数/分类、GATE、1秒资源、0.5秒池/队列/独立PG活动、eventloop lag与body计数；无CPU profiler。SQLAlchemy before_cursor_execute不覆盖driver pre_ping/控制SQL，不能说全SQL为0。独立PG取queryid/阻塞者、backend CPU、phase边界pg_stat_statements/WAL；WAL是clusterwide，PG累计exec不是CPU时间。','','100用户入场/角色准备后，插入readheavy60秒：每用户3轮context/versions/prices/snapshot，计划300次/接口共1200固定读，cap10、轮距20秒/用户错开0.15秒；计划30个低频真实交易循环（18现金6融资6空头，每2秒一次）。随后原readonly30s、normal60s、active45s、stress30s（全体合计25cycles/s）、burst观察10s，条件单跳过。混合阶段已包含readheavy真实交易后的状态，两版相同顺序。脚本/参数/seed相同，不保证异步完成顺序与实际HTTP组成相同；历史补读单列。','','保留原30秒滚动错误率>5%或成功成交P99>2000ms保护，readheavy也适用；提前停止不补跑、不放宽。phase级客户端/SQL/GATE汇总含in-flight收尾；资源/PG activity尽量按真实保护epoch截尾，phase观察有约50ms边界误差。保护后SSE尾值0/0来自驱动停止/取消，不能解释为压力下自发掉线；未独立计数取消请求，不能当0，timeout也不能证明未提交。应用CPU100%约一核，不是整机满。','','初次before启动发生harness lifespan变量名冲突，负载前即失败；已修复，两版使用相同修正脚本。失败日志保留before-startup-failed，原新建before_test库保留，正式均用_r1_test；该次不计入A/B。','','## 读多写少与混合结果','','| 版本 | 阶段 | 成交/循环 | 成交/s | HTTP/s | 成交P99 ms | app CPU %（近似） | SQL总数 | 行情Pair输入SELECT（含版本回退） | public共享锁SQL |','|---|---|---:|---:|---:|---:|---:|---:|---:|---:|']
for v,i in data.items():
 for s in i['stages']:
  t=s['stage'];lines.append(f"| {v} | {s['name']} | {t['successful_trades']}/{t['attempted_trade_cycles']} | {t['successful_trades_per_s']} | {t['completed_requests_per_s']} | {fmt(s['trade_latency'].get('p99_ms'))} | {fmt(s['cpu_mean_percent_approx'])} | {s['sql_total']} | {market(s)} | {guard(s)} |")
for v,i in data.items():lines+=['',f"{v}：停止原因 `{i['abort']}`；保护记录 `{i['protection']}`；未运行阶段 {i['unrun_stages']}；全轮池峰值{i['pool_peak_all']}/30，QueuePool错误{i['pool_error_mentions']}、Traceback{i['tracebacks']}、CancelledError文字{i['cancelled_mentions']}。"]
lines+=['','暖读Pair输入列只统计live_market_read、background:pricing_push、background:public_fx_push的完整/价格输入Pair SELECT；market_data的目录发现/历史维护另列，不包括公共共享锁、业务写/风控/报价、市场目录、个人成交join或history；SQL分类是文本结构启发式，完整route/category仍保留以便核对。公共FOR SHARE从id读取转为pool_version，并没有消除该行锁。after readheavy/readonly/normal的目标行情Pair输入为0；active与stress的public_fx_push分别在324/245次版本guard之后出现23/37次完整Pair回源。read_public_pair_state在锁内发现pool_version与内存peek版本不同时，按设计读取fresh Pair，保证提交与安装内存之间的一致性；这是已设计的fallback，不是0查询路径。普通暖态context/versions/prices/snapshot与pricing_push仍为0，不能将public_fx_push并发回退算成完全消除。账户/依赖/规则SQL与冷态回源允许保留。','','| 版本 | 固定接口 | 完成/计划300 | HTTP结果 | P95 ms | P99 ms |','|---|---|---:|---|---:|---:|']
for v,i in data.items():
 s=next((x for x in i['stages'] if x['name']=='readheavy'),None)
 if not s:continue
 for r in s['read_requests']:
  if not r['name'].startswith('fixed_'):continue
  lat=r['latency_by_result'].get('ok',{});lines.append(f"| {v} | {r['name']} | {sum(r['results'].values())}/300 | {r['results']} | {fmt(lat.get('p95_ms'))} | {fmt(lat.get('p99_ms'))} |")
lines+=['','固定context/snapshot与交易后的页面刷新复用路由；路由总SQL不能直接除固定300次作为单请求SQL。versions/prices在readheavy没有额外周期reader，route计数可直接核对；依赖SQL两版已经是1条UNION，不把先前3→1再次计为本次收益。','','## 交易错误、门闩与连接池','','| 版本 | 阶段 | 冲突 | timeout | 业务拒绝 | 报价不可执行 | 净交易GATE等待P99 ms | 账户context共享GATE等待P99 ms | 队列峰 | 池峰 | lag P99 ms |','|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
for v,i in data.items():
 for s in i['stages']:
  tr=s['trade_results'];w=held(s,'POST /api/v1/fx/pairs/:id/net/trades','wait');cw=held(s,'GET /api/v1/user/trading-context','wait')
  lines.append(f"| {v} | {s['name']} | {tr.get('version_conflict',0)} | {s['http_results'].get('timeout',0)} | {tr.get('business_rejected',0)} | {s['cycle_results'].get('quote_blocked',0)} | {fmt(w.get('p99_ms'))} | {fmt(cw.get('p99_ms'))} | {s['queue_peak']} | {s['pool_peak_phase_sampled']} | {fmt(s['event_loop_lag'].get('p99_ms'))} |")
lines+=['','GATE按每次获取统计，业务可能重试，等待峰值是任务数。账户共享GATE在迁移后参与真实排队，价格缓存命中不代表账户读不会等待写者；各读接口P99与SSE必须一起看，不能只报成交吞吐。','','## SSE与HTTP组成','','| 版本 | 阶段 | SSE在线公开/私人（阶段尾） | 客户端收到帧公开/私人 | chart/history HTTP分类 |','|---|---|---|---|---|']
for v,i in data.items():
 for s in i['stages']:
  t=s['stage'];f=t.get('sse_received',{});lines.append(f"| {v} | {s['name']} | {t['public_connected_at_end']}/{t['private_connected_at_end']} | {f.get('public',{}).get('frames',0)}/{f.get('private',{}).get('frames',0)} | {[(r['name'],r['results']) for r in s['history_http']]} |")
for v,i in data.items():
 lines+=['',f"{v} 全轮SSE新鲜度（依赖同机wallclock，不能当某一阶段分布）："]
 for kind in ('public','private'):
  z=i['sse_global'][kind];lines+=['',f"{kind}：event age {z['event_age_ms_clock_dependent']}；接收间隔 {z['receive_interval']}；counts {z['counts']}。"]
 lines+=['',f"{v} 各阶段单一observer成交可见性见analysis.json trade_visibility；ASGI实际body数/bytes按phase/route另存body-counts.json，可能包含空终止帧，不能等同业务事件数。"]
lines+=['','## 市场SQL范围与PG行锁','','| 版本 | 阶段 | family | Pair输入SELECT | id共享锁 | pool_version共享锁 | metadata SELECT | volume/history SELECT |','|---|---|---|---:|---:|---:|---:|---:|']
for v,i in data.items():
 for s in i['stages']:
  for family in ('live_market_read','background:pricing_push','background:public_fx_push','background:market_data','business_quote_or_write','market_directory_retained','personal_trade_join_retained'):
   c=collections.Counter()
   for r in s['sql_classifications']:
    if r['family']==family:c[r['category']]+=r['executions']
   if not c:continue
   lines.append(f"| {v} | {s['name']} | {family} | {c['fx_current_pair_select']} | {c['fx_public_share_lock']} | {c['fx_pool_version_lock']} | {c['fx_market_metadata_select']} | {c['trade_history_or_volume_select']} |")
lines+=['','/fx/pairs目录发现/metadata、本人的my-trades join Pair及实际交易/风控/net/short/borrow报价保留DB；这些SQL不会被作为未达成暖态目标。market_data后台还负责目录id发现、历史/写入，其Pair SELECT结构计数可能包含select(FxPair.id)，不能把该family所有SQL都当当前市场价格输入。此FX场景不证明LMSR容量。','','| 版本 | 阶段 | PG blocked backend采样条数 | PG CPU秒（近似） | PGexec累计ms | WALsync ms（clusterwide） |','|---|---|---:|---:|---:|---:|']
for v,i in data.items():
 for s in i['stages']:
  d=s.get('pg_delta',{});lines.append(f"| {v} | {s['name']} | {s['pg_blocked_backend_samples']} | {fmt(s['pg_cpu_s_approx'])} | {fmt(d.get('exec_total_ms'))} | {fmt(d.get('wal_clusterwide',{}).get('wal_sync_time'))} |")
lines+=['','0.5s PG活动记录不是锁等待时间占比，短锁等待可能漏采；PG snapshot的实际epoch与queryid/结构SQL、阻塞者保留raw。cursor计数与PG控制SQL不同口径，不能用二者差额推断CPU。没有CPU采样，不作本轮具体函数CPU归因。','','## 验证与回退边界','','readheavy后保留readonly无成交窗口，观测读取/帧数；本轮短窗口不能自然验证24h成交量过期，主agent在负载结束后另复用现有`python -m pytest -q tests/test_fx_history.py -k volume_24h`，4 passed/25 deselected（0.84s），覆盖滚动窗口边界、外部flush、未ready与空市场；这是测试时钟/夹具证据，不是自然运行24h到期演练，详见主agentvalidation.md。本harness未通过直接修改已提交行时间戳制造缓存不一致。回退为回退本次读迁移代码并重启，无schema回退；本轮未实际演练生产回退、未增设双读机制。','','脚本快照harness/与每版actual-command.json、源码hash/diff、客户端http/summary.json/requests.jsonl、sql分类、GATE/队列、资源/lag、SSE body、PG活动/snapshot为证据；固定目录/库名已存在，再跑需新路径/库名，不能开箱重跑。无profiling扰动，但仅单轮固定顺序、实际工作量可能不同，不承诺容量倍数或尾延迟。清理状态见cleanup.json。']
(root/'report.md').write_text('\n'.join(lines)+'\n')
