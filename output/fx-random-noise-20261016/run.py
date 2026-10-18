"""Only pure-random order sigma changes: 108, 54, 27 on the shallow slow candidate.
Run: python output/fx-random-noise-20261016/run.py
"""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import csv
import hashlib
import json
import runpy
import time
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from research.fx.reversion.repeated.entry_model import simulate
from research.fx.episodes.run import save_trace
from research.fx.factor_activity.run import clean_json

OUT=Path(__file__).resolve().parent
SOURCE=ROOT/'output/fx-shallow-calibration-20261016'
SEEDS=[20261029,20261030,20261031]
SIGMAS=[108,54,27]
ANALYZE=runpy.run_path(str(ROOT/'output/fx-calibration-20261016/run.py'))['analyze']
VOL=runpy.run_path(str(ROOT/'output/fx-shallow-volatility-20261016/analyze.py'))['analyze']
CHECK=runpy.run_path(str(ROOT/'output/fx-half-pool-20261016/run.py'))['check']


def main():
    config_path=SOURCE/'candidate-a1.8_t2.json'
    candidate=json.loads(config_path.read_text())
    hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in
        [config_path,ROOT/'research/fx/reversion/repeated/entry_model.py',ROOT/'research/fx/activity/model.py',
         ROOT/'research/fx/episodes/model.py',ROOT/'research/fx/reversion/inventory.py',
         ROOT/'research/fx/factor_activity/model.py',ROOT/'research/fx/reversion/repeated/FROZEN_CONFIG.json']}
    manifest=dict(seeds=SEEDS,sigmas=SIGMAS,base_candidate=str(config_path.relative_to(ROOT)),
        only_changed_parameter='order_sigma_foreign',profiles=candidate['profiles'],activity_b=candidate['activity_b'],
        trend_multiplier=candidate['trend_multiplier'],tick_seconds=5,ticks=10000,primary_exclusion_ticks=360,
        baseline_reuse='sigma108 paths from prior shallow calibration',protected_sha256=hashes,
        limits=['Three reused research seeds, not fresh validation.','No players, external shocks or fees.',
                'Endogenous activity remains unchanged in parameters, so realized arrivals can differ.'])
    (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    results={};traces={};rows=[]
    for sigma in SIGMAS:
        name=f'sigma{sigma}';results[name]={};traces[name]={}
        for seed in SEEDS:
            cfg={**candidate['parameters'],'order_sigma_foreign':sigma,'seeds':[seed]}
            params=dict(config=cfg,profiles=candidate['profiles'],activity_b=candidate['activity_b'],trend_multiplier=candidate['trend_multiplier'])
            if sigma==108:
                src=SOURCE/'a1.8_t2'/f'seed{seed}'
                assert json.loads((src/'parameters.json').read_text())==params
                with np.load(src/'trace.npz') as z:x={k:z[k] for k in z.files}
                s=json.loads((src/'summary.json').read_text());episodes=json.loads((src/'trace-episodes.json').read_text())
                print('Reusing',name,seed,flush=True)
            else:
                print('Running',name,seed,flush=True);start=time.monotonic()
                x,s,episodes,fills,_,_=simulate(cfg,candidate['profiles'],5,candidate['activity_b'],seed,{},'weak_trend',
                    {'trend_multiplier':candidate['trend_multiplier']})
                folder=OUT/name/f'seed{seed}';folder.mkdir(parents=True,exist_ok=True)
                save_trace(folder,'trace',x,episodes,fills)
                for label,value in [('parameters',params),('summary',s)]:
                    (folder/f'{label}.json').write_text(json.dumps(clean_json(value),indent=2)+'\n')
                print('Finished in',round(time.monotonic()-start,2),'seconds',flush=True)
            CHECK(x,s,cfg)
            d=ANALYZE(x,episodes,s);v,_,_=VOL(x['prices'])
            results[name][str(seed)]=dict(market=d,volatility=v)
            traces[name][seed]=x
            horizons=v['horizons'];minutes=9640/12
            row=dict(sigma=sigma,seed=seed,range30=d['range30']['median'],range60=d['range60']['median'],
                abs_net15=horizons['180']['absolute_net_return']['median'],abs_net30=d['abs_net30']['median'],
                std5s=horizons['1']['net_return_std'],std1m=horizons['12']['net_return_std'],std30m=horizons['360']['net_return_std'],
                rv1m=horizons['12']['realized_volatility']['median'],rv30m=horizons['360']['realized_volatility']['median'],
                efficiency15=horizons['180']['direction_retention'],efficiency30=horizons['360']['direction_retention'],
                vr30=horizons['360']['variance_ratio'],cycle05=d['swings']['0.005']['cycles_minutes']['median'],
                cycle1=d['swings']['0.01']['cycles_minutes']['median'],
                trades_per_minute=d['trades_per_minute'],clipped_fraction=d['clipped_fraction'],
                random_quantity_per_minute=float(x['random_executed_abs_quantity'][361:].sum()/minutes),
                inventory_quantity_per_minute=float(x['reversion_executed_abs_quantity'][361:].sum()/minutes),
                short_quantity_per_minute=float(x['factor_executed_abs_quantity'][361:].sum()/minutes),
                holding_median=d['completed_holding_minutes']['median'],unfinished=d['unfinished'],
                short_realized_per_account=d['short_realized_per_account'],max_abs_displacement=d['max_abs_displacement'],
                rv_acf6_30=v['acf']['minute_rv']['mean_lags6_30'])
            rows.append(row)
    averages={str(sigma):{key:float(np.mean([row[key] for row in rows if row['sigma']==sigma]))
        if all(row[key] is not None for row in rows if row['sigma']==sigma) else None
        for key in rows[0] if key not in ['sigma','seed']} for sigma in SIGMAS}
    ratios=[]
    for sigma in SIGMAS[1:]:
        for seed in SEEDS:
            b=next(r for r in rows if r['sigma']==108 and r['seed']==seed)
            c=next(r for r in rows if r['sigma']==sigma and r['seed']==seed)
            ratios.append(dict(sigma=sigma,seed=seed,**{key:c[key]/b[key] for key in
                ['rv1m','rv30m','abs_net15','abs_net30','std30m','efficiency30','range30']}))
    with (OUT/'comparison.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=rows[0]);writer.writeheader();writer.writerows(rows)
    (OUT/'metrics.json').write_text(json.dumps(clean_json(dict(per_seed=results,seed_equal_averages=averages,paired_ratios=ratios)),indent=2)+'\n')
    plot(traces,averages)
    for p,d in hashes.items():assert hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==d,p
    (OUT/'audit.json').write_text(json.dumps(dict(reused_paths=3,new_paths=6,no_shocks=True,
        only_random_order_sigma_changed=True,source_hashes_unchanged=True,assets_conserved=True,
        finite_series_and_nonnegative_wallets=True,valuation_and_trade_counts_verified=True,
        endpoint_returns_and_realized_variance_identities_verified=True),indent=2)+'\n')
    print('Averages:',json.dumps(averages),flush=True)
    print('Paired ratios:',json.dumps(ratios),flush=True)


def plot(traces,averages):
    plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False,'font.size':10})
    colors=['#176997','#b27a16','#a14870']
    fig,axes=plt.subplots(3,3,figsize=(16,10),sharex=True,layout='constrained')
    for i,seed in enumerate(SEEDS):
        ps=[traces[f'sigma{s}'][seed]['prices'][:1001] for s in SIGMAS]
        lo=min(p.min() for p in ps);hi=max(p.max() for p in ps);pad=(hi-lo)*.06
        for j,(sigma,color) in enumerate(zip(SIGMAS,colors)):
            x=traces[f'sigma{sigma}'][seed];ax=axes[i,j]
            ax.plot(np.arange(1001),x['prices'][:1001],color=color,lw=1)
            ax.plot(np.arange(1,1001),x['slow_reference'][1:1001],color='#555',ls='--',lw=.8)
            ax.set(title=f'seed {seed}，随机订单σ={sigma}',ylabel='金圆券／外币',xlabel='tick（5秒）',ylim=(lo-pad,hi+pad),xlim=(0,1000))
            ax.grid(alpha=.18)
    fig.suptitle('仅减小纯随机订单：浅池50万／10万，其他慢候选参数不变\n前1000tick（83分20秒），每行价格轴一致；虚线为库存参考价',fontsize=14)
    fig.savefig(OUT/'paths-1000ticks.png',dpi=150);plt.close(fig)
    fig,axes=plt.subplots(2,2,figsize=(13,8),layout='constrained')
    for ax,keys,title,unit in zip(axes.flat,[['rv1m','rv30m'],['abs_net15','abs_net30'],['efficiency15','efficiency30'],['range30']],
        ['实现波动率中位数','固定持有时间的净变化绝对值中位数','方向保留率','30分钟高低波幅中位数'],['%','%','%','%']):
        labels={'rv1m':'1分钟','rv30m':'30分钟','abs_net15':'15分钟','abs_net30':'30分钟','efficiency15':'15分钟','efficiency30':'30分钟','range30':'30分钟'}
        for idx,key in enumerate(keys):
            ax.plot(range(3),[averages[str(s)][key]*100 for s in SIGMAS],marker='o',color=colors[idx],label=labels[key])
        ax.set(title=title,xlabel='纯随机订单标准差（外币）',ylabel=unit,xticks=range(3),xticklabels=SIGMAS,ylim=(0,None));ax.grid(alpha=.18);ax.legend()
    fig.suptitle('三个种子等权平均，剔除启动30分钟；不年化',fontsize=14)
    fig.savefig(OUT/'metrics.png',dpi=150);plt.close(fig)


if __name__=='__main__':main()
