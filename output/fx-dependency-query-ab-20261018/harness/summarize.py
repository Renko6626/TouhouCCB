import json,collections
from pathlib import Path
root=Path('/data/sunyunbo/www/TouhouCCB/output/fx-dependency-query-ab-20261018');result={}
for variant in ('before','after'):
 p=root/variant
 if not (p/'http/summary.json').exists() or not (p/'gates.json').exists():continue
 raw=json.loads((p/'http/summary.json').read_text());gates=json.loads((p/'gates.json').read_text());sql=json.loads((p/'sql-counts.json').read_text());resources=[json.loads(l) for l in (p/'resources.jsonl').read_text().splitlines()];queue=[json.loads(l) for l in (p/'gate-queue.jsonl').read_text().splitlines()]
 info={'abort':raw['abort_reason'],'pool_peak':max(x['pool_peak_since_sample'] for x in resources),'queue_peak':max(x['waiting'] for x in queue),'stages':[]}
 log=(p/'backend.log').read_text();info['pool_errors']=log.count('QueuePool limit');info['tracebacks']=log.count('Traceback (most recent call last)');info['cancelled_mentions']=log.count('CancelledError')
 for stage in raw['stages']:
  phase=stage['name']
  if phase not in ('readonly','normal','active','stress','burst'):continue
  trade=next((x for x in raw['requests'] if x['phase']==phase and x['name']=='trade'),None)
  counts=collections.Counter()
  for x in raw['requests']:
   if x['phase']==phase and x['name']!='cycle' and not x['name'].startswith('sse_'):counts.update(x['results'])
  gg={x['kind']:x for x in gates if x['phase']==phase and x['route']=='POST /api/v1/fx/pairs/:id/net/trades'}
  qq=[x for x in queue if x['phase']==phase]
  tt=[x for x in resources if qq and qq[0]['t']<=x['t']<=qq[-1]['t']]
  info['stages'].append({'name':phase,'duration_s':stage['duration_s'],'cycles':stage['attempted_trade_cycles'],'successes':stage['successful_trades'],'successes_per_s':stage['successful_trades_per_s'],'nominal_trade_cycles_per_s':stage.get('nominal_trade_cycles_per_s'),'http_results':dict(counts),'trade_results':trade['results'] if trade else {},'trade_latency':trade['latency_by_result'].get('ok',{}) if trade else {},'gate_wait':gg.get('wait'),'gate_held':gg.get('held'),'queue_peak':max((x['waiting'] for x in qq),default=0),'pool_peak_sampled':max((x['pool_checked_out'] for x in qq),default=0),'cpu_mean':sum(x['app_cpu_percent'] for x in tt)/len(tt) if tt else None,'trade_sql':sum(x['executions'] for x in sql if x['phase']==phase and x['route']=='POST /api/v1/fx/pairs/:id/net/trades'),'sse_public':stage['public_connected_at_end'],'sse_private':stage['private_connected_at_end']})
 result[variant]=info
(root/'comparison.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
lines=['# 百人混合压测：显式融资与已锁用户借款优化对照','','本机隔离PostgreSQL14，每版新建独立库，单Uvicorn worker，连接池10+20；客户端/服务端/数据库同机，HTTP仅127.0.0.1:18543。双方均包含短认证session与context依赖复用；全backend文件对照确认仅net_trading.py、loan_service.py不同。源码摘要见每版code-sha256.json。没有访问线上。','','100用户，每人2条SSE（公开/私人各100），角色60%现金、20%融资、20%空头；初始现金100000、源池储备1000000/200000、零FX费率、相同配置与随机种子20261017。异步时序及准备时间不同，不能保证逐笔交易完全一致。双方使用相同轻量SQL计数、0.5秒队列采样、1秒资源采样与门闩时间挂钩，没有CPU profiler。','','阶段：冒烟10s、入场间隔0.3s、只读30s、正常60s、活跃45s、高压30s、集中下单后观察10s；条件单阶段跳过。保留原30秒滚动错误率>5%/成交P99>2s保护。高压目标为100用户合计25次交易循环尝试/s，并非每人25次。每个交易循环包含报价、交易和页面读取，循环不一定到达写请求或成功成交。短阶段仅作优化前后对照，不能作为长期容量验收。','','| 版本 | 阶段 | 成交成功/循环 | 成交/s | 成交P99 ms | HTTP分类 | SSE公开/私人 |','| --- | --- | --- | ---: | ---: | --- | --- |']
for variant,info in result.items():
 for s in info['stages']:lines.append(f"| {variant} | {s['name']} | {s['successes']}/{s['cycles']} | {s['successes_per_s']} | {s['trade_latency'].get('p99_ms','—')} | {s['http_results']} | {s['sse_public']}/{s['sse_private']} |")
lines+=['','| 版本 | 阶段 | 门闩获取数 | 每次获取平均占用 ms | 获取等待P99 ms | 队列峰值 | 成交路由SQL执行数 |','| --- | --- | ---: | ---: | ---: | ---: | ---: |']
for variant,info in result.items():
 for s in info['stages']:
  if not s['gate_held']:continue
  h,w=s['gate_held'],s['gate_wait'];lines.append(f"| {variant} | {s['name']} | {h['count']} | {h['mean_ms']:.2f} | {w['p99_ms']:.2f} | {s['queue_peak']} | {s['trade_sql']} |")
for variant,info in result.items():lines+=['',f"{variant}：停止原因 `{info['abort']}`；全轮池占用峰值 {info['pool_peak']}/30；队列采样峰值 {info['queue_peak']}；池耗尽文字 {info['pool_errors']}次，异常堆栈 {info['tracebacks']}次，CancelledError文字 {info['cancelled_mentions']}次。"]
lines+=['','门闩计时按每次获取计，融资/有债/空头的重试会增加获取次数。优化减少首次失败获取后，即使总工作量减少，每次获取平均占用也未必同比下降。SQL按执行当时阶段、门闩按等待开始阶段、客户端按请求/循环阶段归属，跨阶段及客户端超时后仍在执行的请求会造成边界差异；不能用表内成交成功数直接除SQL推导所有交易的固定SQL。队列峰值是任务数，不是人数。','','保护停止后的SSE 0/0包含驱动主动取消；客户端timeout不能证明交易未提交，没有自动重试写请求。版本冲突单独列出。驱动退出1可能是保护停测，不能直接解释为运行脚本故障或修复验收失败。','','原始证据在每版http/summary.json、http/requests.jsonl、gates.json、gate-queue.jsonl、sql-counts.json、resources.jsonl和backend.log；复现入口`backend/venv/bin/python /tmp/sunyunbo/fx-dependency-query-ab-20261018/run.py`。没有业务代码变更、提交或部署。']
(root/'report.md').write_text('\n'.join(lines)+'\n')
print(json.dumps(result,ensure_ascii=False,indent=2))
