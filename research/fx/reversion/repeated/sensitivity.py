"""One predeclared psi change, preserving the original repeated-shock experiment."""
import csv
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

from research.fx.episodes.run import save_trace
from research.fx.factor_activity.run import ROOT, clean_json
from .metrics import diagnostics, ranges
from .model import simulate
from .run import OUTPUT as BASE


HERE = Path(__file__).parent
OUTPUT = BASE/'sensitivity-psi0008'


def measure(x, summary, event_ticks):
    returns = x['returns'][1:].copy()
    quiet = np.ones(len(returns), dtype=bool)
    ticks = np.arange(1,len(returns)+1)
    for t in event_ticks:
        quiet &= ~((ticks>=t)&(ticks<=t+60))
    rows, curves = [], []
    for i,e in enumerate(summary['events']):
        t=e['tick'];pre=float(e['pre_price']);post=float(e['post_price'])
        returns[t-1]=np.log(pre/x['prices'][t-1])
        end=summary['events'][i+1]['tick']-1 if i+1<len(summary['events']) else len(x['prices'])-1
        q=(x['prices'][t:end+1]-pre)/(post-pre)
        hits=np.flatnonzero(q<=.5)
        sustained=[k for k in range(1,len(q)-11) if np.all(q[k:k+12]<=.5)]
        w=slice(t+1,t+11)
        followed=t+10<len(x['prices'])
        rows.append(dict(tick=t,requested_return=e['return_requested'],
            first_half_minutes=float(hits[0]/6) if len(hits) else None,
            sustained_half_start_minutes=float(sustained[0]/6) if sustained else None,
            retraced_10m=float((post-x['prices'][t+49:t+61].mean())/(post-pre)) if t+60<len(x['prices']) else None,
            first10_mean_intensity=float(x['intensity'][w].mean()) if followed else None,
            first10_arrivals=int(x['arrivals'][w].sum()) if followed else None,
            first10_trades=int(x['trades'][w].sum()) if followed else None,
            first10_distinct_traders=int(np.count_nonzero(x['account_trades'][w].sum(axis=0))) if followed else None))
        if len(q)==1000:
            curves.append(1-q)
    volume=x['executed_abs_quantity'][1:].sum()
    behavior=summary['behavior']
    result=dict(returns=diagnostics(returns),
        range30_all=ranges(x['prices']),range30_quiet=ranges(x['prices'],quiet),
        mean_intensity=float(x['intensity'][1:].mean()), max_intensity=float(x['intensity'][1:].max()),
        max_arrivals=int(x['arrivals'][1:].max()), trades=int(x['trades'][1:].sum()),
        clipped_per_attempt=float(x['clipped_orders'][1:].sum()/x['order_attempts'][1:].sum()),
        executed_over_intended=float(volume/x['raw_abs_demand'][1:].sum()),
        holding_median_minutes=behavior['median_completed_holding']/6,
        holding_mean_minutes=behavior['mean_completed_holding']/6,
        short_realized_pnl=behavior['realized_pnl'],unfinished=behavior['unfinished'],
        final_price=float(x['prices'][-1]), final_bot_wealth=float(x['account_wealth'][-1].sum()),
        events=rows)
    if rows:
        observed=[r for r in rows if r['retraced_10m'] is not None]
        result.update(mean_retraced_10m=float(np.mean([r['retraced_10m'] for r in observed])),
            events_half_reached=sum(r['first_half_minutes'] is not None for r in rows),
            events_half_sustained=sum(r['sustained_half_start_minutes'] is not None for r in rows),
            mean_first10_intensity=float(np.mean([r['first10_mean_intensity'] for r in observed])),
            mean_first10_trades=float(np.mean([r['first10_trades'] for r in observed])),
            mean_first10_distinct_traders=float(np.mean([r['first10_distinct_traders'] for r in observed])))
    return result,np.mean(curves,axis=0) if curves else None


def main():
    manifest=json.loads((BASE/'manifest.json').read_text())
    sources={**manifest['source_sha256'],**manifest['protected_sha256'],**manifest['old_control_prefix_sha256']}
    for p in BASE.iterdir():
        if p.is_file():sources[str(p.relative_to(ROOT))]=hashlib.sha256(p.read_bytes()).hexdigest()
    for path,digest in sources.items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest,path
    cfg={**manifest['config'],'psi':.0008}
    assert [k for k in cfg if cfg[k]!=manifest['config'][k]]==['psi']
    spec={**manifest['spec'],'psi':.0008}
    schedule={int(t):jump for t,jump in manifest['schedule'].items()}
    OUTPUT.mkdir(exist_ok=True)
    result={};saved={};curves={};summaries={}
    old_summary=json.loads((BASE/'summary.json').read_text())
    for case,sched in [('control',{}),('shock',schedule)]:
        x,s,episodes,fills,inventory_fills=simulate(cfg,manifest['profiles'],spec['inventory_count'],spec['b'],spec['market_seed'],sched)
        assert all(np.isfinite(v).all() for v in x.values())
        assert x['account_gold'].min()>=0 and x['account_foreign'].min()>=0
        np.testing.assert_allclose(x['account_wealth'],x['account_gold']+x['account_foreign']*x['prices'][:,None],atol=1e-8,rtol=0)
        np.testing.assert_allclose(x['gold']+x['account_gold'].sum(axis=1),1100000+np.cumsum(x['injection_gold']),atol=1e-8,rtol=0)
        np.testing.assert_allclose(x['pool_foreign']+x['account_foreign'].sum(axis=1),220000+np.cumsum(x['injection_foreign']),atol=1e-8,rtol=0)
        np.testing.assert_array_equal(x['account_trades'].sum(axis=1),x['trades'])
        if case=='shock':
            for e in s['events']:assert abs(float(e['post_price'])/float(e['pre_price'])-1-schedule[e['tick']])<1e-11
            for key in x:np.testing.assert_array_equal(x[key][:1000],saved['control'][key][:1000])
            assert s['events'][-1]['tick']==10000
        save_trace(OUTPUT,case,x,episodes,fills)
        (OUTPUT/f'{case}-inventory.jsonl').write_text(''.join(json.dumps(f)+'\n' for f in inventory_fills))
        summaries[case]=s;saved[case]=x
        for variant in ['old','new']:
            if variant=='old':
                with np.load(BASE/f'{case}.npz') as z:series={k:z[k] for k in z.files}
                summary=old_summary[case]
            else:series=x;summary=s
            metrics,curve=measure(series,summary,sorted(schedule))
            result[f'{variant}_{case}']=metrics
            if curve is not None:curves[variant]=curve
        print('Complete',case,'mean intensity',result[f'new_{case}']['mean_intensity'],flush=True)
    for path,digest in sources.items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest,path
    (OUTPUT/'manifest.json').write_text(json.dumps(dict(spec=spec,config=cfg,profiles=manifest['profiles'],schedule=schedule,
        baseline_protected_sha256=sources,source_sha256={str((HERE/'sensitivity.py').relative_to(ROOT)):hashlib.sha256((HERE/'sensitivity.py').read_bytes()).hexdigest()}),indent=2)+'\n')
    (OUTPUT/'summary.json').write_text(json.dumps(clean_json(summaries),indent=2)+'\n')
    (OUTPUT/'metrics.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    with (OUTPUT/'events.csv').open('w') as f:
        rows=[dict(variant=v,**row) for v in ['old','new'] for row in result[f'{v}_shock']['events']]
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False})
    fig,axes=plt.subplots(3,1,figsize=(12,10))
    with np.load(BASE/'shock.npz') as z:old_prices=z['prices']
    for ax,case in zip(axes[:2],['control','shock']):
        with np.load(BASE/f'{case}.npz') as z:ax.plot(np.arange(10001)/360,z['prices'],label='原psi=0.0012',lw=1,color='#798a9c')
        ax.plot(np.arange(10001)/360,saved[case]['prices'],label='高敏感psi=0.0008',lw=1,color='#145887')
        ax.set(title='无冲击对照' if case=='control' else '相同±5%事件日程',xlabel='小时',ylabel='价格')
        if case=='shock':
            for t in schedule:ax.axvline(t/360,color='gray',alpha=.15)
    for variant,label in [('old','原psi=0.0012'),('new','高敏感psi=0.0008')]:
        axes[2].plot(np.arange(1000)/6,curves[variant]*100,label=label)
    axes[2].axhline(50,ls='--',color='gray');axes[2].set(xlabel='事件后分钟',ylabel='平均原始回吐（%）',title='9次事件等权平均；不同事件相关，未扣除长期偏移')
    for ax in axes:ax.legend();ax.grid(alpha=.2)
    fig.tight_layout();fig.savefig(OUTPUT/'comparison.png',dpi=150);plt.close(fig)
    lines=['# 仅上调活动敏感性：psi 0.0012 → 0.0008','','只改psi，1/psi提高50%。在给定同一段收益历史时，z放大1.5倍、平方反馈项放大2.25倍；内生路径会改变，实际热度不保证同比例变化。b=.25、tau1000、三组策略、订单120和钱包均不变。市场种子81、人口301、冲击种子20261015，同一10次±5%日程。注资金额随当时池储备变化，不是固定金额配对。','','旧两条路径直接读取，新参数运行无冲击和连续冲击各10000tick。第10000tick冲击无后续，事件均值只含前9次。相同种子不意味着轨迹分叉后逐笔交易配对。','','|指标|原无冲击|新无冲击|原连续冲击|新连续冲击|','|---|---:|---:|---:|---:|']
    fields=[('平均到达强度/tick',lambda d:f"{d['mean_intensity']:.3f}"),('最大到达强度/tick',lambda d:f"{d['max_intensity']:.3f}"),
        ('单tick最大实际到达数',lambda d:str(d['max_arrivals'])),('成交笔数',lambda d:str(d['trades'])),
        ('避开事件及随后10分钟的30分钟波幅中位数',lambda d:f"{d['range30_quiet']['median']:.2%}"),
        ('收益ACF lag1',lambda d:f"{d['returns']['acf']['signed']['1_1']:.5f}"),
        ('绝对收益ACF31–180',lambda d:f"{d['returns']['acf']['abs']['31_180']:.5f}"),
        ('平方收益ACF31–180',lambda d:f"{d['returns']['acf']['sq']['31_180']:.5f}"),
        ('超额峰度（含零收益）',lambda d:f"{d['returns']['shape']['excess_kurtosis']:.2f}"),
        ('裁剪/实际订单尝试',lambda d:f"{d['clipped_per_attempt']:.2%}"),('实际/意向绝对量',lambda d:f"{d['executed_over_intended']:.2%}"),
        ('短线中位持仓/分钟',lambda d:f"{d['holding_median_minutes']:.2f}"),('短线已实现盈亏',lambda d:d['short_realized_pnl'])]
    for label,fmt in fields:lines.append('|'+label+'|'+'|'.join(fmt(result[k]) for k in ['old_control','new_control','old_shock','new_shock'])+'|')
    lines+=['','收益统计中，冲击组仅替换10次事件tick为发行前收益，保留随后反应与时序；不是无冲击收益。完整30分钟波幅仍含事件后的价格路径，剔除掩码仅用来挑选完整窗口。ACF沿用common.diagnostics有偏估计；无预热，含所有零收益。','','|事件响应（9次）|原参数|高敏感参数|','|---|---:|---:|']
    for field,label,fmt in [('mean_first10_intensity','事件后100秒平均到达强度/tick','.3f'),('mean_first10_trades','事件后100秒平均成交笔数','.2f'),
        ('mean_first10_distinct_traders','事件后100秒平均成交人数','.2f'),('mean_retraced_10m','10分钟平均原始回吐','.1%'),
        ('events_half_reached','下次冲击前曾回吐一半的事件数','.0f'),('events_half_sustained','回吐一半保持12个收盘的事件数','.0f')]:
        lines.append('|'+label+'|'+'|'.join(format(result[v+'_shock'][field],fmt) for v in ['old','new'])+'|')
    lines+=['','10分钟回吐使用第9～10分钟12个收盘均价；半幅按冲击前后价格中点，严格在下次事件前截断，不扣除长期偏移。持续半幅用连续12个收盘，时间记首个收盘。单路径9个事件不是独立样本，不能据此宣布稳健性。','','![对照](comparison.png)','','保存新两条完整npz、个人钱包矩阵、短线轮次、短线/库存成交账本及终态。逐tick核对两币守恒、钱包非负、个人净值和成交笔数；首个事件前新组两路径完全一致；原报告、旧结果及其原始依赖哈希保持。','','复现：`python -m research.fx.reversion.repeated.sensitivity`。详细数据见[metrics.json](metrics.json)、[逐次响应](events.csv)、[设置与原文件哈希](manifest.json)。','']
    (OUTPUT/'report.md').write_text('\n'.join(lines))
    print('Verified: baseline unchanged, two-asset conservation, wallet marks, event sizes, causal prefix.',flush=True)


if __name__=='__main__':main()
