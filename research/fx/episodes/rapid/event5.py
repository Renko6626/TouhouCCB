"""A single +5% reserve event using saved normal controls and unchanged bots."""
from decimal import Decimal
import csv
import hashlib
import json

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

from research.fx.episodes.run import simulate, save_trace, source_hashes
from research.fx.factor_activity.run import ROOT, clean_json
from .run import HERE

OUTPUT=HERE/'results/event5'


def main():
    OUTPUT.mkdir(parents=True,exist_ok=True)
    t0=1000;rate=Decimal('.05');seeds=[81,82,83]
    cells=[('short_0125','±0.125%',HERE/'results','eighth_rate1'),
           ('moderate_025','±0.25%',HERE.parent/'session/results','exit_quarter')]
    sources=source_hashes()
    sources[str((HERE/'event5.py').relative_to(ROOT))]=hashlib.sha256((HERE/'event5.py').read_bytes()).hexdigest()
    spec=dict(event_tick=t0,event_price_jump=.05,shock_direction='up_only',tick_seconds=10,
              horizons=[2160,8640],observation_ticks=[30,60,180],seeds=seeds,random_count=15,b=.25,
              note='isolated event; no calibrated event frequency or downward-event test')
    records=[];actions=[];responses=[];illustrations={};controls={}
    for cell,label,base,original_id in cells:
        manifest=json.loads((base/'manifest.json').read_text())
        for p,h in manifest['source_sha256'].items():assert hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h,p
        for seed in seeds:
            name=f'{original_id}-random15-seed{seed}';old=base/name
            record=json.loads((old/'result.json').read_text());cfg=record['config']
            with np.load(old/'control.npz') as z:control={k:z[k] for k in z.files}
            pre_gold=Decimal(record['control_summary']['issue']['pre_gold'])
            amount=float((pre_gold*rate).quantize(Decimal('.000001')))
            shock,summary,episodes,fills=simulate(cfg,manifest['profiles'],15,.25,seed,'trailing',t0,amount)
            assert all(np.array_equal(shock[k][:t0],control[k][:t0]) for k in control)
            pre=float(summary['issue']['pre_price']);post=float(summary['issue']['post_price'])
            assert abs(post/pre-1-.05)<1e-11
            np.testing.assert_allclose(shock['gold']+shock['random_gold']+shock['factor_gold'],
                1100000+np.cumsum(shock['injection_gold']),rtol=0,atol=1e-8)
            np.testing.assert_allclose(shock['pool_foreign']+shock['random_net_position']+shock['factor_net_position'],200000,rtol=0,atol=1e-8)
            assert len({(f['account'],f['tick']) for f in fills})==len(fills)
            directory=OUTPUT/f'{cell}-seed{seed}';directory.mkdir(exist_ok=True)
            save_trace(directory,'shock',shock,episodes,fills)
            (directory/'summary.json').write_text(json.dumps(clean_json(summary),ensure_ascii=False,indent=2)+'\n')
            controls[str((old/'control.npz').relative_to(ROOT))]=hashlib.sha256((old/'control.npz').read_bytes()).hexdigest()
            controls[str((old/'result.json').relative_to(ROOT))]=hashlib.sha256((old/'result.json').read_bytes()).hexdigest()
            records.append(dict(cell=cell,label=label,seed=seed,control_directory=str(old.relative_to(ROOT)),
                                injection_gold=amount,config=cfg,summary=summary))
            carried=[e for e in episodes if e['entry_tick']<=t0 and (e['close_tick'] is None or e['close_tick']>t0)]
            for window in spec['observation_ticks']:
                end=t0+window;fs=[f for f in fills if t0<f['tick']<=end]
                closed=[e for e in episodes if e['close_tick'] is not None and t0<e['close_tick']<=end]
                newly_closed=[e for e in closed if e['entry_tick']>t0]
                actions.append(dict(cell=cell,label=label,seed=seed,minutes=window/6,
                    carried_episodes=len(carried),carried_closed=sum(e['close_tick'] is not None and e['close_tick']<=end for e in carried),
                    entries=sum(f['action']=='open' for f in fs),completed=len(closed),
                    newly_entered_completed=len(newly_closed),
                    newly_completed_mean_holding_minutes=float(np.mean([(e['close_tick']-e['entry_tick'])/6 for e in newly_closed])) if newly_closed else None,
                    long_profit_sell=sum(float(f['quantity']) for f in fs if f['action']=='close' and f['direction']==1 and f['reason']=='take_profit'),
                    inventory_stop_rebuy=sum(float(f['quantity']) for f in fs if f['action']=='close' and f['direction']==-1 and f['reason']=='stop_loss'),
                    behavior_net_buy=sum(f['actual_flow'] for f in fs),
                    peak_intensity=float(shock['intensity'][t0+1:end+1].max()),
                    max_arrivals=int(shock['arrivals'][t0+1:end+1].max())))
            for t in spec['horizons']:
                ps=float(shock['prices'][t-179:t+1].mean());pc=float(control['prices'][t-179:t+1].mean())
                hits=np.flatnonzero(shock['prices'][t0+1:t+1]<=pre)
                responses.append(dict(cell=cell,label=label,seed=seed,hours=t/360,terminal_price_change=ps/pre-1,
                    raw_retraced=(post-ps)/(post-pre),paired_retraced=1-(ps-pc)/(post-pre),
                    first_full_hit=int(hits[0]+1) if len(hits) else None,
                    peak_intensity=float(shock['intensity'][t0+1:t+1].max()),max_arrivals=int(shock['arrivals'][t0+1:t+1].max())))
            if seed==81:illustrations[cell]=(shock,control,pre)
            print('Complete',cell,seed,'injection',amount,flush=True)
    for name,rows in [('actions.csv',actions),('responses.csv',responses)]:
        with (OUTPUT/name).open('w') as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    (OUTPUT/'manifest.json').write_text(json.dumps(dict(spec=spec,source_sha256=sources,control_sha256=controls),indent=2)+'\n')
    (OUTPUT/'summary.json').write_text(json.dumps(clean_json(records),ensure_ascii=False,indent=2)+'\n')
    plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False,'font.size':10})
    fig,axes=plt.subplots(2,2,figsize=(12,7))
    for j,(cell,label,_,_) in enumerate(cells):
        x,c,pre=illustrations[cell];end=t0+180
        hours=np.arange(t0-30,end+1)/6
        axes[0,j].plot(hours-t0/6,(x['prices'][t0-30:end+1]/pre-1)*100,label='+5%事件')
        axes[0,j].plot(hours-t0/6,(c['prices'][t0-30:end+1]/pre-1)*100,label='无事件配对',ls=':')
        axes[0,j].set_title(label+'退出阈值');axes[0,j].set_ylabel('相对注资前价格（%）');axes[0,j].legend()
        axes[1,j].plot(hours-t0/6,np.cumsum(x['completions'][t0-30:end+1]))
        axes[1,j].set_ylabel('5个行为账户累计完成轮数')
        for ax in axes[:,j]:ax.axvline(0,color='gray',ls=':');ax.set_xlabel('距事件分钟');ax.grid(alpha=.2)
    fig.suptitle('单次+5%事件前后：混合组，原到达率，固定展示种子81')
    fig.tight_layout();fig.savefig(OUTPUT/'response.png',dpi=140);plt.close(fig)
    def select(data,cell,key,value):return [r for r in data if r['cell']==cell and r[key]==value]
    def avg(rows,key):return float(np.mean([r[key] for r in rows]))
    lines=['# 单次+5%事件：5～15分钟短线候选的响应','',
        '用户明确：目标典型持仓5～15分钟，5%指单次典型大事件涨跌。本轮沿用现有央行注资情形，只验证一次向上+5%的事件，不推断下跌对称性或连续事件效果。','',
        '## 设置','',
        '15随机＋5行为、移动退出、原到达基准1、tau1000、人口301及行情81/82/83；其余池、钱包、零费用、100外币交易轮与此前相同。比较±.125%候选与±.25%较慢参照，移动启动/回撤各为止盈止损绝对值的一半。正常路径直接复用旧序列。','',
        '第1000tick常规成交后，按实际池金圆券储备的5%注入（取六位小数），所以即时价格约上涨5%，每条注入金额稍有不同。六条新路径各8640tick，51840tick；6小时与24小时按前缀统计，含开局、不丢预热。表格等权平均三个种子；5/10/30分钟指事件之后，而非游戏开局。','',
        '## 事件后动作','',
        '|阈值|事件后分钟|冲击时原有持仓数|原有持仓完成退出数|新入场轮数|全部完成轮数|其中事件后新建且已完成轮数|',
        '|---|---:|---:|---:|---:|---:|---:|']
    for cell,label,_,_ in cells:
        for minutes in [5,10,30]:
            g=select(actions,cell,'minutes',minutes)
            lines.append(f"|{label}|{minutes}|{avg(g,'carried_episodes'):.2f}|{avg(g,'carried_closed'):.2f}|{avg(g,'entries'):.2f}|{avg(g,'completed'):.2f}|{avg(g,'newly_entered_completed'):.2f}|")
    lines+=['','![5%事件前后](response.png)','',
        '原有持仓上涨止盈卖出或减持后止损买回，新一轮随后按新的真实成本管理。事件期活动增加可以暂时缩短持有；5～15分钟是常态下的典型目标，没有硬性最短/最长持仓。首次退出后继续发生新交易，不能把所有事件后完成轮都当成原有持仓。','',
        '## 局末价格与活动','',
        '|阈值|局长小时|末30分钟均价仍高于事件前|原始回落|配对回落|三个种子最大预定强度|最大单tick到达数|',
        '|---|---:|---:|---:|---:|---:|---:|']
    for cell,label,_,_ in cells:
        for hours in [6,24]:
            g=select(responses,cell,'hours',hours)
            lines.append(f"|{label}|{hours}|{avg(g,'terminal_price_change'):.2%}|{avg(g,'raw_retraced'):.1%}|{avg(g,'paired_retraced'):.1%}|{max(r['peak_intensity'] for r in g):.2f}|{max(r['max_arrivals'] for r in g)}|")
    lines+=['','原始回落100%代表回到事件前旧价，配对回落100%代表追上同期无事件价格，两者不同。永久增加池储备后，没有预设旧价吸引力；短线退出不保证恢复旧价。最大值是本次观测，不是硬上限。尚未测实际游戏时延。','',
        '## 复现和边界','',
        '运行`OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python -m research.fx.episodes.rapid.event5`。六条新路径的注资前全部字段与保存控制一致，跳价约5%（误差<1e-11），逐tick资产守恒、钱包非负及每户每tick最多一笔真实成交核对通过；原模拟器内部同时核对成本、残量与钱包。策略与公共代码未改，已有24项研究检查此前通过。','',
        '- [事件后动作](actions.csv)、[逐路径响应](responses.csv)、[完整设置及账户结果](summary.json)、[来源与控制哈希](manifest.json)。','']
    (OUTPUT/'report.md').write_text('\n'.join(lines))
    for p,h in {**sources,**controls}.items():assert hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h,p
    print('Six isolated +5% events complete; existing controls unchanged.',flush=True)


if __name__=='__main__':main()
