"""Read saved paths at actual game horizons; never tune or rerun the model."""
import csv
import hashlib
import json
from decimal import Decimal

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

from research.fx.common.diagnostics import acf
from research.fx.episodes.run import longest_empty
from research.fx.factor_activity.run import ROOT, clean_json
from .run import HERE, OUTPUT


def write_csv(name, rows):
    with (OUTPUT/name).open('w') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)


def main():
    manifest=json.loads((OUTPUT/'manifest.json').read_text());spec=manifest['spec']
    records=json.loads((OUTPUT/'summary.json').read_text())
    assert len(records)==36 and all(r['status']=='complete' for r in records)
    for p,h in {**manifest['source_sha256'],**manifest['protected_sha256']}.items():
        assert hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h,p
    rows=[];responses=[];trace={};ledger_checks=0
    for r in records:
        count=r['random_count'];n=20-count;directory=OUTPUT/r['name']
        cases={}
        for label in ['control','shock']:
            with np.load(directory/f'{label}.npz') as z: x={k:z[k] for k in z.files}
            cases[label]=x
            episodes=json.loads((directory/f'{label}-episodes.json').read_text())
            fills=[json.loads(s) for s in (directory/f'{label}-fills.jsonl').read_text().splitlines()]
            for t in spec['horizons']:
                opened=[e for e in episodes if e['entry_tick']<=t]
                done=[e for e in opened if e['close_tick'] is not None and e['close_tick']<=t]
                done_ids={e['id'] for e in done}
                pending=[e for e in opened if e['id'] not in done_ids]
                per=[sum(e['account']==str(i) for e in done) for i in range(count,20)]
                holdings=[(e['close_tick']-e['entry_tick'])*10/60 for e in done]
                ages=[(t-e['entry_tick'])*10/60 for e in pending]
                fs=[f for f in fills if f['tick']<=t]
                assert len(done)==int(x['completions'][1:t+1].sum())
                assert len(opened)==int(x['entries'][1:t+1].sum())
                assert abs(float(sum((Decimal(f['realized_profit']) for f in fs),Decimal(0)))-x['realized_pnl'][t])<1e-8
                by_episode={}
                for f in fs:by_episode.setdefault(f['episode_id'],[]).append(f)
                for e in opened:
                    ef=by_episode[e['id']]
                    residual=Decimal(e['entry_quantity'])-sum((Decimal(f['quantity']) for f in ef if f['action']=='close'),Decimal(0))
                    assert (residual==0)==(e['id'] in done_ids)
                    ledger_checks+=1
                q=x['executed_abs_quantity'][1:t+1].sum()
                active=np.flatnonzero(x['trades'][1:t+1])
                a=x['returns'][1:t+1]
                curves={k:acf(v,360) for k,v in [('signed',a),('abs',abs(a)),('sq',a*a)]} if label=='control' else {}
                row=dict(cell=r['cell']['id'],label=r['cell']['label'],random_count=count,seed=r['seed'],
                         case=label,hours=t*10/3600,completed=len(done),completed_per_bot=len(done)/n,
                         zero_completed_accounts=sum(v==0 for v in per),unfinished=len(pending),
                         mean_completed_holding_minutes=float(np.mean(holdings)) if holdings else None,
                         median_completed_holding_minutes=float(np.median(holdings)) if holdings else None,
                         p90_completed_holding_minutes=float(np.quantile(holdings,.9)) if holdings else None,
                         max_pending_age_minutes=max(ages) if ages else None,
                         last_trade_tick=int(active[-1]+1) if len(active) else None,
                         longest_no_trade_minutes=longest_empty(x['trades'][1:t+1])/6,
                         behavior_volume_share=float(x['factor_executed_abs_quantity'][1:t+1].sum()/q) if q else None,
                         realized_episode_pnl=float(x['realized_pnl'][t]),
                         arrivals=int(x['arrivals'][1:t+1].sum()),max_arrivals=int(x['arrivals'][1:t+1].max()),
                         peak_intensity=float(x['intensity'][1:t+1].max()),
                         clipped_per_attempt=float(x['clipped_orders'][1:t+1].sum()/x['order_attempts'][1:t+1].sum()),
                         wallet_gold_min=float(x['wallet_gold_min'][:t+1].min()),wallet_foreign_min=float(x['wallet_foreign_min'][:t+1].min()),
                         signed_lag1=float(curves['signed'][1]) if curves else None,
                         max_signed_1_100=float(np.max(abs(curves['signed'][1:101]))) if curves else None,
                         abs_31_180=float(curves['abs'][31:181].mean()) if curves else None,
                         sq_31_180=float(curves['sq'][31:181].mean()) if curves else None)
                rows.append(clean_json(row))
            if r['seed']==81:trace[r['cell']['id'],count,label]=x
        t0=spec['shock_tick'];pre=float(r['shock_summary']['issue']['pre_price']);post=float(r['shock_summary']['issue']['post_price'])
        assert all(np.array_equal(cases['shock'][k][:t0],cases['control'][k][:t0]) for k in cases['control'])
        for t in spec['horizons']:
            x,c=cases['shock'],cases['control'];w=spec['terminal_window']
            ps=x['prices'][t-w+1:t+1].mean();pc=c['prices'][t-w+1:t+1].mean()
            hits=np.flatnonzero(x['prices'][t0+1:t+1]<=pre)
            responses.append(dict(cell=r['cell']['id'],label=r['cell']['label'],random_count=count,seed=r['seed'],hours=t/360,
                terminal_price_change=float(ps/pre-1),raw_retraced=float((post-ps)/(post-pre)),
                paired_retraced=float(1-(ps-pc)/(post-pre)),first_full_hit_ticks=int(hits[0]+1) if len(hits) else None,
                post_peak_intensity=float(x['intensity'][t0+1:t+1].max()),max_post_arrivals=int(x['arrivals'][t0+1:t+1].max()),
                behavior_net_sold=float(-x['factor_actual_flow'][t0+1:t+1].sum())))
    assert len(rows)==144 and len(responses)==72
    write_csv('sessions.csv',rows);write_csv('responses.csv',responses)
    plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False,'font.size':10})
    colors=plt.get_cmap('tab10').colors
    fig,axes=plt.subplots(2,2,figsize=(13,8))
    for j,t in enumerate(spec['horizons']):
        for i,cell in enumerate(spec['cells']):
            x=trace[cell['id'],15,'control'];hours=np.arange(t+1)/360
            axes[0,j].plot(hours,(x['prices'][:t+1]/x['prices'][0]-1)*100,color=colors[i],label=cell['label'],lw=1)
            axes[1,j].plot(hours,np.cumsum(x['completions'][:t+1])/5,color=colors[i],label=cell['label'],lw=1.4)
        axes[0,j].set_title(f'开局至{t/360:g}小时');axes[0,j].set_ylabel('相对初价变化（%）')
        axes[1,j].set_ylabel('每个行为账户累计完成轮数')
        for ax in axes[:,j]:ax.set_xlabel('游戏小时');ax.grid(alpha=.2)
    axes[0,0].legend(fontsize=8);fig.suptitle('15随机＋5行为：固定展示种子81；6小时是24小时路径的前缀')
    fig.tight_layout();fig.savefig(OUTPUT/'normal.png',dpi=140);plt.close(fig)
    fig,axes=plt.subplots(2,2,figsize=(13,8))
    for j,t in enumerate(spec['horizons']):
        for i,cell in enumerate(spec['cells']):
            x=trace[cell['id'],15,'shock']
            record=next(r for r in records if r['seed']==81 and r['random_count']==15 and r['cell']['id']==cell['id'])
            pre=float(record['shock_summary']['issue']['pre_price'])
            hours=np.arange(t0-30,t+1)/360
            axes[0,j].plot(hours,(x['prices'][t0-30:t+1]/pre-1)*100,color=colors[i],label=cell['label'],lw=1)
            axes[1,j].plot(hours,x['intensity'][t0-30:t+1],color=colors[i],lw=1)
        axes[0,j].set_title(f'注资后观察至第{t/360:g}小时');axes[0,j].set_ylabel('相对各自注资前价格（%）')
        axes[1,j].set_ylabel('预定到达强度（次/tick）')
        for ax in axes[:,j]:ax.axvline(t0/360,color='gray',ls=':');ax.set_xlabel('游戏小时');ax.grid(alpha=.2)
    axes[0,0].legend(fontsize=8);fig.suptitle('10万注资：混合组、种子81；表格另列配对对照校正，图示为实际注资路径')
    fig.tight_layout();fig.savefig(OUTPUT/'shock.png',dpi=140);plt.close(fig)

    def selected(data,cell,count,hours,case=None):
        return [r for r in data if r['cell']==cell and r['random_count']==count and r['hours']==hours and (case is None or r['case']==case)]
    def avg(group,key):
        a=[r[key] for r in group]
        return float(np.mean(a)) if a and all(v is not None for v in a) else None
    def fmt(v,d=2):return '未定义' if v is None else f'{v:.{d}f}'
    lines=['# 更短线的机器人：退出阈值与查看频率探索','',
        '在用户要求进一步缩短持仓后，冻结六组更低退出阈值与到达率比较，沿用原有行为代码；旧版本和上一轮五组参数均保留。此处区分持有时间与整轮间隔，完成轮数包括等待下一次入场的影响。','',
        '## 设置与口径','',
        '1tick为10秒，6小时2160tick、24小时8640tick。20户、池100万金圆券/20万外币、户均5000/1000、零费用、每轮100外币；个人三因子30/30窗口、人口301/spread1、热度tau1000、b=.25保持。仅移动退出版。三种止盈止损绝对阈值为.125%、.0625%、.025%；移动启动和回撤各为其一半。原到达基准1与现有mean_arrivals=3作比较，其他规则不变，见[实验说明](../README.md)和[配置](../config.json)。','',
        '提高mean_arrivals会同时提高随机和行为账户的到达机会，并通过价格改变反馈路径；不是单独给行为账户增加监控。Lambda=(1−b)*mean_arrivals+b*z²，因此三倍基准并不保证任意tick的实际到达或成交恰好三倍。仍在到达时观察，退出后最早下tick再入场。','',
        '行情81/82/83、20行为及15随机＋5行为，6组共36对/72路径、622080tick。统计从开局开始，不删预热；6小时是24小时的同一路径前缀，不是独立重复。所有表先逐路径计算再平均三个行情种子；已完成持有均值不包含期末未完成的长持仓，二者在原始表分列。并非未见种子验证或人类行为校准。','',
        '## 混合组：有多短线','',
        '|止盈止损／到达基准|6小时完成轮/户|24小时完成轮/户|24小时已完成轮平均持有分钟|已完成轮中位持有分钟|24小时行为成交占比|',
        '|---|---:|---:|---:|---:|---:|']
    for cell in spec['cells']:
        a=selected(rows,cell['id'],15,6,'control');b=selected(rows,cell['id'],15,24,'control')
        lines.append(f"|{cell['label']}|{avg(a,'completed_per_bot'):.2f}|{avg(b,'completed_per_bot'):.2f}|{fmt(avg(b,'mean_completed_holding_minutes'))}|{fmt(avg(b,'median_completed_holding_minutes'))}|{avg(b,'behavior_volume_share'):.1%}|")
    lines+=['','![混合组价格与完成轮数](normal.png)','',
        '相同tau、相同原到达基准的上一轮参照：±.25%时，6小时每户14.27轮、24小时64.00轮、已完成持仓均值15.55分钟；±.5%时分别8.07轮、30.87轮、39.28分钟。参照来自相同人口与行情种子的[局长实验](../../session/results/report.md)，没有重跑或改写。','',
        '很低阈值可能让一两笔其他订单触发退出。更高轮数不能直接说明更像人、盈利或玩家更容易应对；零手续费对这种短线模式尤其有利，加入实际收费后必须另行核验。','',
        '## 用户明确的5～15分钟目标',
        '',
        '用户随后选定典型持仓5～15分钟，并明确5%指单次典型大事件。原到达率、±.125%记为候选：三个种子已完成持仓均值8.19～8.28分钟，中位数6.33～6.50分钟，6小时每个行为账户平均24轮；移动启动和回撤各.0625个百分点。此选择未替换生产或旧研究基线。',
        '',
        '目标描述典型时间，没有强制每轮在区间内结束：24小时已完成轮中约49%～52%持有5～15分钟、35%～38%不足5分钟、12.5%～13.7%超过15分钟；各路径90分位约16.4～17.2分钟。期末还有1～4轮未完成，最老约13～34分钟，不能将其删除或当完成。见[candidate-distribution.json](candidate-distribution.json)及[候选配置](../candidate-5-15min.json)。',
        '',
        '5%单次事件与整局振幅分开：该候选常态6小时高低价差/初价约2.56%～3.27%，24小时约4.19%～4.93%，后者恰好接近5%不等于已模拟单次5%大事件，见[candidate-price-scale.json](candidate-price-scale.json)。此前10万注资约跳10%，作为压力情形保留。',
        '',
        '另用实际池金圆券储备的5%注入，补跑了[单次+5%事件](event5/report.md)：候选组事件发生时的旧持仓在5分钟内全部完成退出，5个行为账户在事件后30分钟合计平均完成60.33轮。热度让事件期明显快于常态；事件频率尚未设定，不能直接宣称整局都符合5～15分钟目标。仅验证上涨，不推断下跌对称性或连续事件效果。',
        '',
        '## 全行为组是否仍会停住',
        '',
        '|参数|6小时完成轮/户|24小时完成轮/户|末1小时完全无成交的路径数/3|最后成交时点范围（开局后小时）|',
        '|---|---:|---:|---:|---:|---:|']
    for cell in spec['cells']:
        a=selected(rows,cell['id'],0,6,'control');b=selected(rows,cell['id'],0,24,'control')
        last=[r['last_trade_tick']/360 for r in b]
        lines.append(f"|{cell['label']}|{avg(a,'completed_per_bot'):.2f}|{avg(b,'completed_per_bot'):.2f}|{sum(v<=23 for v in last)}/3|{min(last):.2f}～{max(last):.2f}|")
    for rate in [1,3]:
        for seed in spec['market_seeds']:
            for label in ['control','shock']:
                with np.load(OUTPUT/f'sixteenth_rate{rate}-random00-seed{seed}'/f'{label}.npz') as a, np.load(OUTPUT/f'fortieth_rate{rate}-random00-seed{seed}'/f'{label}.npz') as b:
                    assert all(np.array_equal(a[k],b[k]) for k in a.files)
    lines+=['','全行为组把阈值从±.0625%降至±.025%，三个种子的常态与注资保存序列逐字段相同（两个到达基准共12对）。在这组100外币订单与池深度下，进一步缩小阈值未改变执行，不能假设更小阈值会无限缩短持仓。','',
        '|全行为组参数|24小时收益ACF lag1|abs(r) ACF31–180|',
        '|---|---:|---:|']
    for cell in spec['cells']:
        g=selected(rows,cell['id'],0,24,'control')
        lines.append(f"|{cell['label']}|{fmt(avg(g,'signed_lag1'),5)}|{fmt(avg(g,'abs_31_180'),5)}|")
    lines+=['','全行为组持续成交与保留波动聚集是两个目标；本轮其幅度相关接近零，部分更快组的负方向相关更明显。混合组也有取舍：最激进的3倍到达、±.025%组收益lag1约−.066，不能继续称为涨跌几乎无相关。','']
    lines+=['','未加超时退出、补钱、价格锚或多种策略。末1小时是否成交只描述当前窗口，不能保证未来永远持续；阈值足够低时，机器人之间的价格冲击也可能维持频繁反向交易，需要结合方向相关判断。','',
        '## 常态市场诊断与资金约束','',
        '以下是24小时混合组。ACF含零收益、按整局全局均值计算，使用滞后乘积和/零滞后平方和，不作无偏修正；常数未定义。方向最大值取lag1–100；幅度和平方取31–180tick（约5–30分钟）。短路径下这些是描述性指标，不作不可预测或严格长记忆结论。裁剪分母为实际订单尝试数，持有等待不计入订单。','',
        '|参数|收益ACF lag1|收益ACF最大绝对值1–100|abs(r) ACF31–180|r² ACF31–180|订单裁剪比例|',
        '|---|---:|---:|---:|---:|---:|---:|']
    for cell in spec['cells']:
        g=selected(rows,cell['id'],15,24,'control')
        lines.append('|'+cell['label']+'|'+'|'.join(fmt(avg(g,k),5) for k in ['signed_lag1','max_signed_1_100','abs_31_180','sq_31_180'])+f"|{avg(g,'clipped_per_attempt'):.2%}|")
    lines+=['','## 央行注资后的本局表现','',
        '第1000tick（开局2小时46分40秒）常规成交后向池本币侧加10万。末段取局末180tick/30分钟均价，相对各自注资前即时价格；原始回落100%指回旧价，配对回落100%指追上同期无注资路径，不可混用。下表均为混合组；峰值是三个种子最大值，不是模型硬上限。','',
        '|参数|6小时末仍高于旧价|24小时末仍高于旧价|24小时配对回落|最大预定强度|最大单tick到达数|',
        '|---|---:|---:|---:|---:|---:|']
    for cell in spec['cells']:
        a=selected(responses,cell['id'],15,6);b=selected(responses,cell['id'],15,24)
        lines.append(f"|{cell['label']}|{avg(a,'terminal_price_change'):.2%}|{avg(b,'terminal_price_change'):.2%}|{avg(b,'paired_retraced'):.1%}|{max(r['post_peak_intensity'] for r in b):.1f}|{max(r['max_post_arrivals'] for r in b)}|")
    lines+=['','![注资价格与活动](shock.png)','',
        '价格路径由真实成交产生，永久加储备改变池状态，不保证更短线就恢复旧价。没有测定实际游戏运行时延，不能把到达峰值当性能认证。未实现损益与底仓市值不混入已实现轮次利润；完整路径结局保留在summary及逐笔账本。','',
        '## 验证与复现','',
        f'72路径完成；注资前逐字段一致、逐tick两币守恒、成本和现金关系、钱包非负、每户每tick最多一笔成交核对通过。按两个局长另核对{ledger_checks}个截断交易轮的数量/残量与已实现现金损益，报告仅计入当时已经完成的轮次；{len(manifest["protected_sha256"])}个既有文件哈希未变。研究原有24项检查本轮运行通过，未改策略代码，未新增永久测试。','',
        '复现：`OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python -m research.fx.episodes.rapid.run`，随后`python -m research.fx.episodes.rapid.report`。','',
        '- [全部6/24小时行为及市场指标](sessions.csv)、[注资配对指标](responses.csv)、[路径和账户终态](summary.json)、[冻结参数及源哈希](manifest.json)。各路径目录保存序列、交易轮及行为成交。','']
    (OUTPUT/'report.md').write_text('\n'.join(lines))
    paths=[HERE/'report.py',OUTPUT/'summary.json',OUTPUT/'manifest.json',OUTPUT/'sessions.csv',OUTPUT/'responses.csv',OUTPUT/'report.md',HERE/'candidate-5-15min.json',OUTPUT/'candidate-distribution.json',OUTPUT/'candidate-price-scale.json']
    (OUTPUT/'report_sources.json').write_text(json.dumps({str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},indent=2)+'\n')
    print('Report complete: 144 horizon rows, 72 response rows;',ledger_checks,'ledger-prefix checks.')


if __name__=='__main__':main()
