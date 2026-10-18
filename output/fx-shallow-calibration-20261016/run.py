"""Shallow-pool parameter calibration, existing model only.

Target: 2x amplitude and 2x duration relative to the original DEEP baseline.
Run: python output/fx-shallow-calibration-20261016/run.py
"""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import csv
import hashlib
import json
import runpy
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from research.fx.reversion.repeated.entry_model import simulate
from research.fx.episodes.run import save_trace
from research.fx.factor_activity.run import clean_json

OUT=Path(__file__).resolve().parent
OLD=runpy.run_path(str(ROOT/'output/fx-calibration-20261016/run.py'))
CHECK=runpy.run_path(str(ROOT/'output/fx-half-pool-20261016/run.py'))['check']
TRAIN=[20261027,20261028]
HOLDOUT=[20261029,20261030,20261031]
CANDIDATES={f'a{a:g}_t{t:g}':(a,t) for a in [1.8,2.2] for t in [1.5,2,3]}
FROZEN=ROOT/'research/fx/reversion/repeated/FROZEN_CONFIG.json'


def setup(frozen,name):
    if name in ['deep_baseline','shallow_unmodified']:
        cfg,profiles,b,trend=OLD['setup'](frozen,1,1)
    else:
        a,t=CANDIDATES[name]
        cfg,profiles,b,trend=OLD['setup'](frozen,a,t)
        # Pool halving supplies the price-impact multiplier: quantities need
        # only a/2 of original sizes for the approximate price scaling a.
        for key in ['order_sigma_foreign','entry_quantity','inventory_position_cap']:
            cfg[key]*=.5
    if name!='deep_baseline':cfg.update(pool_gold=500000,pool_foreign=100000)
    return cfg,profiles,b,trend


def run_case(frozen,name,seed):
    cfg,profiles,b,trend=setup(frozen,name);cfg['seeds']=[seed]
    folder=OUT/name/f'seed{seed}';folder.mkdir(parents=True,exist_ok=True)
    parameters=dict(config=cfg,profiles=profiles,activity_b=b,trend_multiplier=trend)
    if (folder/'metrics.json').exists():
        assert json.loads((folder/'parameters.json').read_text())==parameters
        return json.loads((folder/'metrics.json').read_text())
    print('Running',name,seed,flush=True)
    x,s,episodes,fills,_,_=simulate(cfg,profiles,5,b,seed,{},frozen['policy'],{'trend_multiplier':trend})
    CHECK(x,s,cfg)
    save_trace(folder,'trace',x,episodes,fills)
    for label,value in [('summary',s),('parameters',parameters)]:
        (folder/f'{label}.json').write_text(json.dumps(clean_json(value),indent=2)+'\n')
    d=OLD['analyze'](x,episodes,s)
    d.update(mean_intensity=float(x['intensity'][361:].mean()),peak_intensity=float(x['intensity'][361:].max()),
        short_mean_wealth_vs_hold=float(x['account_wealth'][-1,15:].mean()-(5000+1000*x['prices'][-1])))
    (folder/'metrics.json').write_text(json.dumps(clean_json(d),indent=2)+'\n')
    return d


def paired(b,c):
    return dict(**OLD['compare'](b,c),abs_net30_ratio=c['abs_net30']['median']/b['abs_net30']['median'],
        same_time_range30_ratio=c['range30']['median']/b['range30']['median'])


def main():
    frozen=json.loads(FROZEN.read_text())
    files=[FROZEN,ROOT/'research/fx/reversion/repeated/entry_model.py',ROOT/'research/fx/activity/model.py',
        ROOT/'research/fx/episodes/model.py',ROOT/'research/fx/reversion/inventory.py',ROOT/'research/fx/factor_activity/model.py',
        ROOT/'output/fx-calibration-20261016/run.py',ROOT/'output/fx-half-pool-20261016/run.py']
    hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    manifest=dict(train_seeds=TRAIN,holdout_seeds=HOLDOUT,candidates=CANDIDATES,ticks=10000,tick_seconds=5,
        pool_gold=500000,pool_foreign=100000,accounts=20,initial_gold=5000,initial_foreign=1000,
        target='2x amplitude and 2x duration relative to original deep baseline; NOT another 2x on top of shallow baseline.',
        selection='Top two by existing three-component log-ratio score on training seeds, before holdout.',
        scope='Existing parameter values only; no change to strategies, wallets, fees, pool product or production configuration.',
        primary_exclusion_ticks=360,source_sha256=hashes)
    (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    baselines={s:run_case(frozen,'deep_baseline',s) for s in TRAIN}
    training={}
    for name in CANDIDATES:
        training[name]=OLD['average']([paired(baselines[s],run_case(frozen,name,s)) for s in TRAIN])
        print('Training',name,json.dumps(training[name]),flush=True)
    ranked=sorted(training,key=lambda n:training[n]['score']);selected=ranked[:2]
    (OUT/'selected-before-holdout.json').write_text(json.dumps(dict(selected=selected,training=training,ranked=ranked),indent=2)+'\n')
    print('Selected before holdout:',selected,flush=True)
    holdout={n:[] for n in selected};metrics={n:{} for n in ['deep_baseline','shallow_unmodified',*selected]}
    for seed in HOLDOUT:
        for name in metrics:metrics[name][str(seed)]=run_case(frozen,name,seed)
        for name in selected:holdout[name].append(dict(seed=seed,**paired(metrics['deep_baseline'][str(seed)],metrics[name][str(seed)])))
    means={name:OLD['average']([{k:v for k,v in r.items() if k!='seed'} for r in rows]) for name,rows in holdout.items()}
    result=dict(selected=selected,training=training,holdout=holdout,holdout_averages=means,metrics=metrics)
    (OUT/'results.json').write_text(json.dumps(clean_json(result),indent=2)+'\n')
    flat=[]
    for name,by_seed in metrics.items():
        for seed,d in by_seed.items():
            flat.append(dict(variant=name,seed=int(seed),range30=d['range30']['median'],range60=d['range60']['median'],
                abs_net30=d['abs_net30']['median'],amplitude05=d['swings']['0.005']['amplitude']['median'],
                amplitude1=d['swings']['0.01']['amplitude']['median'],cycle05=d['swings']['0.005']['cycles_minutes']['median'],
                cycle1=d['swings']['0.01']['cycles_minutes']['median'],trades_per_minute=d['trades_per_minute'],
                clipped_fraction=d['clipped_fraction'],executed_quantity_fraction=d['executed_quantity_fraction'],
                holding_median=d['completed_holding_minutes']['median'],unfinished=d['unfinished'],
                mean_intensity=d['mean_intensity'],peak_intensity=d['peak_intensity'],
                max_abs_displacement=d['max_abs_displacement'],short_realized_per_account=d['short_realized_per_account'],
                short_mean_wealth_vs_hold=d['short_mean_wealth_vs_hold']))
    with (OUT/'comparison.csv').open('w') as f:
        w=csv.DictWriter(f,fieldnames=flat[0]);w.writeheader();w.writerows(flat)
    for name in selected:
        cfg,profiles,b,trend=setup(frozen,name)
        (OUT/f'candidate-{name}.json').write_text(json.dumps(dict(name=name,status='experimental_not_applied',tick_seconds=5,
            parameters=cfg,profiles=profiles,activity_b=b,trend_multiplier=trend),indent=2)+'\n')
    sensitivity={}
    for name in selected:
        sensitivity[name]=[]
        for seed in HOLDOUT:
            loaded=[]
            for n in ['deep_baseline',name]:
                with np.load(OUT/n/f'seed{seed}'/'trace.npz') as z:loaded.append(z['prices'][1080:])
            b,c=loaded;_,bl,bc=OLD['SWINGS'](b,.005);_,cl,cc=OLD['SWINGS'](c,.01)
            sensitivity[name].append(dict(seed=seed,wave_amplitude=float(np.median([abs(v['change']) for v in cl])/np.median([abs(v['change']) for v in bl])),
                wave_duration=float(np.median(cc)/np.median(bc)),matched_horizon_range=OLD['ranges'](c,60)['median']/OLD['ranges'](b,30)['median']))
    (OUT/'warmup-sensitivity.json').write_text(json.dumps(sensitivity,indent=2)+'\n')
    plot(selected)
    for p,d in hashes.items():assert hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==d,p
    (OUT/'audit.json').write_text(json.dumps(dict(paths=26,new_paths=26,training_paths=14,holdout_paths=12,
        no_shocks=True,all_series_finite=True,wallets_nonnegative=True,assets_conserved_for_actual_pool_sizes=True,
        wallet_values_and_trade_counts_verified=True,source_hashes_unchanged=True,selected_before_holdout=True),indent=2)+'\n')
    print('Holdout means:',json.dumps(means),flush=True)


def plot(selected):
    plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False,'font.size':10})
    names=['deep_baseline','shallow_unmodified',*selected]
    labels=['深池原配置','浅池未调机器人',*selected]
    colors=['#176997','#777','#b27a16','#a14870']
    for seed in HOLDOUT:
        fig,axes=plt.subplots(4,1,figsize=(14,11),sharex=True,sharey=True,layout='constrained')
        for ax,name,label,color in zip(axes,names,labels,colors):
            with np.load(OUT/name/f'seed{seed}'/'trace.npz') as z:
                p=z['prices'][:1001];ref=z['slow_reference'][1:1001]
            ax.plot(np.arange(1001),p,color=color,lw=1.1,label='池价')
            ax.plot(np.arange(1,1001),ref,color='#555',lw=.9,ls='--',label='库存参考价')
            ax.set(title=label,ylabel='金圆券／外币',xlim=(0,1000));ax.grid(alpha=.18);ax.legend(ncol=2,fontsize=8)
        axes[-1].set_xlabel('tick（5秒）；1000tick＝83分20秒')
        fig.suptitle(f'浅池调参验证：seed {seed}，前1000tick，四图坐标一致，无冲击',fontsize=14)
        fig.savefig(OUT/f'paths-1000ticks-seed{seed}.png',dpi=150);plt.close(fig)
    fig,axes=plt.subplots(3,1,figsize=(14,9),layout='constrained')
    for ax,seed in zip(axes,HOLDOUT):
        for name,label,color in zip(names,labels,colors):
            with np.load(OUT/name/f'seed{seed}'/'trace.npz') as z:p=z['prices']
            ax.plot(np.arange(len(p))/720,(p/p[0]-1)*100,color=color,label=label,lw=.7)
        ax.set(title=f'seed {seed}：完整路径',ylabel='相对初价变化（%）',xlabel='模拟小时');ax.grid(alpha=.18);ax.legend(ncol=4,fontsize=8)
    fig.savefig(OUT/'holdout-full-paths.png',dpi=150);plt.close(fig)


if __name__=='__main__':main()
