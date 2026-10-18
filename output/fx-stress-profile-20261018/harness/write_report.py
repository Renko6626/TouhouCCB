from pathlib import Path
import json
root=Path('/data/sunyunbo/www/TouhouCCB/output/fx-stress-profile-20261018');data=json.loads((root/'analysis.json').read_text());fmt=lambda n:f'{n:.2f}' if isinstance(n,(int,float)) else '—'
lines=['# 100用户高压诊断：CPU与等待时间','','直接排队点是同一FX品种的写门闩：control完整窗口事件平均等候6.35s、持有87.28ms，等待任务峰101；profile平均等候7.34s、持有100.16ms，峰108。两轮持有时间总和约29.3/29.5s，接近各自30秒观察窗口，写临界区持续繁忙；池未耗尽，不能把连接池作为本轮首要队列。profile应用CPU约88.95%，control约97.56%；blocking外部采样会暂停目标，同时实际完成工作量也不同，不能把CPU比例下降理解为应用变轻。profile仪器有可见干扰；实际历史补读分支也不同：control stress汇总chart_tail292次、history_segment72次，profile均0（含drain口径）。两轮吞吐差额不能用于量化仪器损耗，以上不构成新容量结论。本轮目标是采到真实100人高压并定位，未为精确估计profiler开销补跑。','','CPU没有集中到一个业务算法：严格stress1289个SIGPROF ticks中，SQLAlchemy叶帧34.83%、async调度34.21%，分散在缓存键生成、greenlet边界、调度/取消/transport写等函数；外部native421样本中SQLAlchemy24.94%、async调度26.60%，另gc_collect_main为39样本（9.26%）。外部89 errors且blocking会暂停应用，精确比例不可靠，但两类采样都支持框架/任务/对象处理广泛占用，未支持AMM算术为主热点。','','等待时间也不是纯数据库执行：profile完整持有事件均值100.16ms，cursor elapsed79.22ms、commit-driver2.40ms、其余18.54ms。cursor包含PG、驱动及恢复调度，不能按79ms全算数据库，更不能把其与PGexec差额全算CPU。PG确有FOR SHARE/UPDATE锁等待：control/profile分别采到13/9条blocked backend记录，FOR SHARE累计约3738/3630ms；WAL sync累计373/336ms，读写I/O计时均0（缓存热的本轮，不能外推冷盘）。control两次FOR UPDATE受阻采样中，阻塞者均为idle in transaction/ClientRead，最后query分别在market metadata与rolling24h volume读取；这直接支持读事务可能阻写。13/59次采样见阻塞不是锁等待时间占比。较多idle in transaction/ClientRead说明应用持事务时DB在等客户端；观测到的事实是写门闩持续繁忙、CPU接近单核且分散、存在DB锁阻塞；由此推断应用侧CPU竞争/恢复调度与事务锁持有共同放大临界区elapsed，具体各自份额本轮不能完全分离，也未通过开关实验验证独立因果。','','私人SSE synthetic标签占25.60% ticks，其中188/330个叶样本属async调度；公共SSE9.23%，两条SSE合计约34.8%。本窗口ASGI body消息私人24353、公共22000，确有每次成交后面向百个订阅者的调度/send放大。不能由这些数字直接说JSON是主因：JSON叶帧仅1.86%，pricing_push标签1.40%，未知/native与GC仍存在归因边界。39个gc_collect_main样本的Python父链人工复核中，至少8个经过private frames→is_disconnected→receive_or_disconnect→anyio.__aexit__，至少5个经过该接收链→start_soon→_spawn；这支持任务/取消检查相关分配与GC的线索，不能当作严格独立成本或把39个GC样本全部归给这条链。','','本轮只诊断，不实现优化，内存迁移保持暂停。实验基线HEAD `d88a9399ded066eb517d7eee5fba8b43fffd886a`，包含融资4文件未提交改动及已提交query优化；control/profile全backend源码hash一致，见source-hashes.json。仅本机隔离PG14 Unix socket55489、每轮新建fx_stress_profile_{control,profile}_test；单Uvicorn worker，127.0.0.1:18543，池10+20。未连接生产、未修改业务源、未提交或部署。','','## 方法及窗口','','两轮复用相同负载脚本、参数与seed，实际HTTP组成不同（历史补读/SSE-tail动态分支、异步完成顺序）；不声称逐笔请求完全相同：100用户、公开/私人SSE各100，现金/融资/空头=60/20/20，seed20261017。smoke10s、每用户入场0.3s、readonly30s、normal60s、active45s、stress30s，stress需求为全体合计25交易循环/s。保持原30s滚动错误率>5%或成功成交P99>2000ms保护；不补跑、不放宽阈值。','','两轮stress前均只有同样的SQL执行次数、GATE等候/持有、池峰值、事件循环lag与资源轻量监测。control不启CPU/重型SQLelapsed；profile在server观察到真实stress后启用49Hz SIGPROF、cursor/flush/commit/函数elapsed、pool._do_get计时，同时外部py-spy19Hz --gil --native。两轮性能差异包含全部这些仪器开销，不能全归于CPU采样。独立PG进程不依赖应用event loop，0.5s采集本库pg_stat_activity、queryid、阻塞者与backend CPU，stress边界取pg_stat_statements/WAL快照。','','CPU每tick只在内存记录栈、epoch、route与当前task是否实际持GATE，无文件IO；route/background是synthetic上下文标签，不是完整async调用因果。SIGPROF按进程CPU计时但Python signal可能延后处理、合并；C扩展中的CPU可能记到返回后的Python位置，具体归因需结合外部样本。CPU原始tick按客户端stress开始→保护触发epoch裁剪，shutdown/drain排除。GATE分解只统计requested/acquired/released全部落在窗口的事件；跨界/收尾另列，长等待的删失会使完整事件分位数低估末尾排队，因此同时列原始队列增长。','','应用CPU100%约为单worker占一个核，不是整机耗尽。profile窗口主线程CPU占进程CPU约97.06%，支持主线程样本代表主要进程工作；其余线程CPU仍可能通过进程SIGPROF带来小偏差。事件循环lag（超出50ms tick的延迟）control均值9.13ms/P99 138.85ms/max183.80ms，profile均值11.03ms/P99 132.10ms/max176.39ms。host-start/end记录112逻辑CPU、affinity、cgroup配额/节流与CPU pressure/load；应用、数据库、驱动同机，运行顺序固定、未重复交叉。','','## 场景结果','','| 轮次 | stress窗口s | 循环启动数 | 报价HTTP完成 | 完成写请求 | 成功 | 冲突 | timeout | 业务拒绝 | 报价不可执行 | 成交/s（含drain阶段） | 成功P99 ms（含drain） |','|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
for v,i in data.items():
 req=i.get('requests',[]);trade=next((r for r in req if r['name']=='trade'),{'results':{},'latency_by_result':{}});tr=trade['results'];cycle=next((r['results'] for r in req if r['name']=='cycle'),{});s=i['stress_stage'] or {}
 lines.append(f"| {v} | {fmt(i['stress_window_s'])} | {s.get('attempted_trade_cycles',0)} | {i.get('quote_http_completions',0)} | {sum(tr.values())} | {tr.get('ok',0)} | {tr.get('version_conflict',0)} | {tr.get('timeout',0)} | {tr.get('business_rejected',0)} | {cycle.get('quote_blocked',0)} | {s.get('successful_trades_per_s','—')} | {trade['latency_by_result'].get('ok',{}).get('p99_ms','—')} |")
for v,i in data.items():
 lines += ['',f"{v}：保护原因 `{i['abort']}`；真实stress开始 {i['stress_start']}，停止负载 {i['load_stop']}，stage返回 {i.get('stress_stage_return')}；全轮池峰值{i['pool_peak_all']}/30。未运行阶段：{[s for s in ('stress','burst') if s not in [x['name'] for x in i['all_stages']]]}。"]
lines+=['','循环含报价与页面读，不等于写请求；报价HTTP成功仍可能executable=false。以上客户端stress汇总包含保护后的in-flight drain，不能与严格裁剪CPU/PG窗口直接相除。驱动停止结束200条SSE后尾值0/0，不能解释为负载中自发掉线；HTTP/task取消未单独计数，不能记作零。timeout不能证明服务端未提交。','','## CPU采样与路由','','| 轮次 | 应用CPU秒（采样近似） | 应用CPU % | PG后端CPU秒（近似） | CPU ticks | pool峰值 | GATE等待峰值 |','|---|---:|---:|---:|---:|---:|---:|']
for v,i in data.items():
 r=i.get('stress_resources',{});c=i.get('cpu',{})
 lines.append(f"| {v} | {fmt(r.get('app_cpu_s_approx'))} | {fmt(r.get('app_cpu_percent_approx'))} | {fmt(r.get('pg_cpu_s_approx'))} | {c.get('stress_ticks',0)} | {r.get('pool_peak',0)} | {r.get('gate_queue_peak',0)} |")
 if not c.get('stress_ticks'):continue
 lines+=['',f"{v} SIGPROF errors={c['errors']}，窗口外tick排除={c['excluded_ticks']}。下面是叶帧样本比例；不等于独立函数精确CPU成本。",'','| 叶帧类别 | 样本 | % |','|---|---:|---:|']
 for r in c['leaf_categories']:lines.append(f"| {r['name']} | {r['samples']} | {fmt(r['percent'])} |")
 lines+=['','| synthetic route/background | 样本 | % |','|---|---:|---:|']
 for r in c['routes']:lines.append(f"| {r['name']} | {r['samples']} | {fmt(r['percent'])} |")
 lines+=['','当前task持GATE样本分布：`'+str(c['held_gate'])+'`。','', '| 高占用叶函数 | 样本 | % |','|---|---:|---:|']
 for r in c['top_leaf_functions'][:18]:lines.append(f"| {r['name']} | {r['samples']} | {fmt(r['percent'])} |")
 lines+=['',f"profile主线程CPU/进程CPU比例（近似）：{i.get('stress_resources',{}).get('main_thread_cpu_fraction')}。"]
 n=i.get('native')
 if n:
  lines+=['',f"外部采样：{n['samples']} samples（日志报告89 errors，缺失/错误样本不作为空闲证据）；实际flags为 --rate19 --gil --native，blocking={n['window']['blocking']}，退出status={n['window']['status']}。进程时窗 {n['window']['start']}→{n['window']['stop_requested']}（非逐tick精确时间戳，可能含启动开销/边界几十毫秒）。原始日志：",'','```text',n['log'].strip(),'```','','| 外部叶帧类别 | 样本 | % |','|---|---:|---:|']
  for r in n['leaf_categories']:lines.append(f"| {r['name']} | {r['samples']} | {fmt(r['percent'])} |")
 else:lines+=['','外部采样无有效文件，见native-watch.log/native.log/native-window.json；不把空样本当证据。']
lines+=['','## GATE、连接池与事件循环','','GATE每次获取事件均有requested/acquired/released时间戳。持有内cursor时间包括驱动等待、PG执行、网络和协程重新获调度等；commit-driver从SQLAlchemy commit开始到after_commit，含驱动和调度；remaining=held-cursor-commit，包含Python、pool pre_ping/获取及其他await/调度，不能称为纯CPU。flush inclusive与cursor重叠，不能再相加。pool._do_get是实际获取路径的elapsed，含连接创建/调度，且不包含随后pool_pre_ping，因此不是纯连接池队列等待。','','| 轮次 | 完整/记录GATE | 等候P99 ms | 持有均值ms | cursor均值ms | commit均值ms | remaining均值ms |','|---|---:|---:|---:|---:|---:|---:|']
for v,i in data.items():
 g=i['gate_events'];d=g['decomposition'];lines.append(f"| {v} | {g['fully_enclosed']}/{g['recorded']} | {fmt(g['wait'].get('p99_ms'))} | {fmt(g['held'].get('mean_ms'))} | {fmt(d['cursor_wait'].get('mean_ms'))} | {fmt(d['commit_driver'].get('mean_ms'))} | {fmt(d['remaining_inclusive'].get('mean_ms'))} |")
for v,i in data.items():
 for name in ('pool_do_get_including_creation_scheduling','event_loop_lag','flush_inclusive','commit_including_flush','discover_dependencies','check_new_risk','_simulate'):
  rr=[r for r in i['timings_enclosed_window'] if r['name']==name];values=[]
  # Summary by route remains inspectable in analysis.json; show critical pool/lag aggregates.
  if rr:lines+=['',f"{v} {name}："+'；'.join(f"{r['route']} n={r['count']} mean={fmt(r['mean_ms'])}ms P99={fmt(r['p99_ms'])}ms" for r in rr)]
 lines+=['',f"{v} 保护后观察：`{i['post_stop_queue_observation']}`。此为驱动停止/取消后的收尾，不构成自然恢复时间。"]
lines+=['','## PostgreSQL执行、I/O与控制语句','','PG语句累计elapsed可跨连接重叠，不是CPU秒；与cursor合计的差额也不是CPU。SQLAlchemy cursor hook不包含asyncpg pool_pre_ping的BEGIN/ping/ROLLBACK等driver内部控制语句；PG端差分包含这类语句。PG_STAT快照由独立进程观察phase/protection后触发，实际窗口延迟下列列出，in-flight未完成语句/统计刷新可能落在收尾快照。pg_stat_activity是0.5s采样，短等待可能漏采。WAL为clusterwide（本次临时cluster，无生产），不强行归因每个用户事务。','','| 轮次 | PGexec累计ms | 读I/O ms | 写I/O ms | WALsync ms | WALwrite ms | blocked backend采样数 | 边界延迟entry/end ms |','|---|---:|---:|---:|---:|---:|---:|---|']
for v,i in data.items():
 d=i.get('pg_delta',{});w=d.get('wal_clusterwide',{});lines.append(f"| {v} | {fmt(d.get('exec_total_ms'))} | {fmt(d.get('read_io_ms'))} | {fmt(d.get('write_io_ms'))} | {fmt(w.get('wal_sync_time'))} | {fmt(w.get('wal_write_time'))} | {i['pg_blocked_backend_samples']} | {fmt(d.get('start_delay_ms'))}/{fmt(d.get('end_delay_ms'))} |")
for v,i in data.items():
 d=i.get('pg_delta',{})
 lines+=['',f"{v} pg_stat_activity：`{i['pg_activity_counts']}`。",'','| calls | PGexec ms | query结构（无参数） |','|---:|---:|---|']
 for q in d.get('queries',[])[:12]:lines.append(f"| {int(q['calls'])} | {fmt(q['total_exec_time'])} | `{(q['query'].replace(chr(10),' ') if len(q['query'])<=260 else q['query'].replace(chr(10),' ')[:185]+' … '+q['query'].replace(chr(10),' ')[-85:])}` |")
lines+=['','| 轮次 | PG控制语句 | calls | PGexec ms |','|---|---|---:|---:|']
for v,i in data.items():
 for q in i.get('pg_delta',{}).get('queries',[]):
  if q['query'].strip().upper() in ('BEGIN','COMMIT','ROLLBACK'):lines.append(f"| {v} | {q['query']} | {int(q['calls'])} | {fmt(q['total_exec_time'])} |")
lines+=['','外部native样本有8/421条栈包含asyncpg dialect的_async_ping（inclusive，不能与叶帧占比相加）。大量BEGIN/ROLLBACK包含业务/会话及pre_ping等控制路径，不能全归于一种机制；cursor hook未覆盖它们，PG COMMIT exec计时也不能替代commit-driver与WAL sync测量。','']
lines+=['','## Fanout计数','','ASGI send只计response.body消息数/bytes，不记录payload；可能包含空终止帧，不能与业务事件数直接相等。记录窗口跟随server stress观察（相对保护最多一个poll/调度周期尾差），不是完整客户端逐帧因果。','','| 轮次 | route | body消息 | bytes |','|---|---|---:|---:|']
for v,i in data.items():
 groups={}
 for r in i['bodies']:
  route='/history/fx/:id/:version/:interval/:segment.json' if r['route'].startswith('/history/') or r['route'].startswith('GET /history/') else r['route']
  c=groups.setdefault(route,{});c[r['kind']]=c.get(r['kind'],0)+r['count']
 for route,c in sorted(groups.items()):lines.append(f"| {v} | {route} | {c.get('frames',0)} | {c.get('bytes',0)} |")
lines+=['','## 证据边界与运行记录','','具体高占用函数、外部叶帧及调用栈保留在analysis.json与profile/cpu.speedscope.json、profile/native-stress.speedscope.json。队列增长/持有数/池占用完整曲线在每轮resources.jsonl，严格stress窗口见stress-started.json、protection.json、instrument-windows.json；pg-activity与snapshot保留queryid、阻塞者和结构化SQL，无参数/token。代码中的FX公共流每帧create/cancel/gather任务、私人流is_disconnected与PricingPublisher全订阅者offer/model_dump只是解释候选，不能把所有async/JSON样本自动归给其中某处；需要以具体栈支持。','','本轮未切换专门stress模式、未新增优化实验、未运行全量或浏览器测试。监测错误：'+str({v:(i['monitor_errors'],i['pg_monitor_errors']) for v,i in data.items()})+'。命令在commands.json，harness是实际临时脚本快照，依赖/tmp旧辅助与保留融资diff的源码；固定目录/库名已存在，不能开箱重跑，需换新路径/库名。新测试库保留，不drop旧库。清理状态见cleanup.json（服务、PG、tokens）。']
(root/'report.md').write_text('\n'.join(lines)+'\n')
