"""Bounded calibration of the frozen FX model: 2x amplitude, 2x wave duration.

Only existing parameters change. No production or frozen configuration writes.
Run: python output/fx-calibration-20261016/run.py
"""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import csv
import hashlib
import json
import math
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
FROZEN_PATH=ROOT/'research/fx/reversion/repeated/FROZEN_CONFIG.json'
HELPERS=runpy.run_path(str(ROOT/'output/fx-timescale-20261016/run.py'))
SWINGS,STATS=HELPERS['swings'],HELPERS['stats']
TRAIN=[20261022,20261023]
HOLDOUT=[20261024,20261025,20261026]
CANDIDATES={f'a{a:g}_t{t:g}':(a,t) for a in [1.8,2.2] for t in [1.5,2,3]}


def setup(frozen, amplitude, duration):
    cfg={**frozen['research_reproduction']['selected_simulator_config'],**frozen['parameters']}
    # Approximate amplitude/time scaling of the existing discrete agent process.
    # Lambda' = Lambda / duration for the same z. This needs BOTH b and the
    # background mean to change, not just the mean-arrivals parameter.
    b=frozen['activity_b']/duration
    cfg.update(mean_arrivals=((1-frozen['activity_b'])*cfg['mean_arrivals']/duration)/(1-b),
        tau=cfg['tau']*duration,psi=cfg['psi']*amplitude/math.sqrt(duration),
        order_sigma_foreign=cfg['order_sigma_foreign']*amplitude,
        entry_quantity=cfg['entry_quantity']*amplitude,
        inventory_position_cap=cfg['inventory_position_cap']*amplitude,
        inventory_sensitivity=cfg['inventory_sensitivity']/amplitude)
    for key in ['trend_window','mean_window','inventory_window']:
        cfg[key]=round(cfg[key]*duration)
    for key in ['take_profit','stop_loss','trail_arm','trail_drawdown']:
        cfg[key]*=amplitude
    # Mean-window deviations scale with amplitude, while average per-tick
    # trend scales as amplitude/duration. Keep relative forecast terms similar.
    profiles=[{**p,'sigma':p['sigma']*amplitude} for p in frozen['profiles']]
    trend_multiplier=frozen['trend_multiplier']*duration
    return cfg,profiles,b,trend_multiplier


def ranges(prices,minutes):
    width=minutes*12;n=(len(prices)-1)//width
    blocks=[prices[i*width:(i+1)*width+1] for i in range(n)]
    return STATS([(p.max()-p.min())/p[0] for p in blocks])


def analyze(x,episodes,summary):
    # Fixed 30m startup exclusion for all cases; does not assert stationarity.
    prices=x['prices'][360:];r=np.diff(np.log(prices));d={}
    for threshold in [.005,.01,.02]:
        _,legs,cycles=SWINGS(prices,threshold)
        d[str(threshold)]=dict(legs_minutes=STATS([v['minutes'] for v in legs]),
            cycles_minutes=STATS(cycles),amplitude=STATS([abs(v['change']) for v in legs]))
    done=[e for e in episodes if e['close_tick'] is not None and e['entry_tick']>=360]
    attempts=x['order_attempts'][361:].sum();raw=x['raw_abs_demand'][361:].sum()
    return dict(swings=d,range30=ranges(prices,30),range60=ranges(prices,60),
        abs_net30=STATS([abs(prices[(i+1)*360]/prices[i*360]-1) for i in range((len(prices)-1)//360)]),
        trades_per_minute=float(x['trades'][361:].sum()/(len(r)/12)),
        clipped_fraction=float(x['clipped_orders'][361:].sum()/attempts),
        executed_quantity_fraction=float(x['executed_abs_quantity'][361:].sum()/raw),
        cash_saturated_arrivals=int(x['cash_saturated'][361:].sum()),
        foreign_saturated_arrivals=int(x['foreign_saturated'][361:].sum()),
        tick_return_std=float(np.std(r)),net_change=float(x['prices'][-1]/x['prices'][0]-1),
        max_abs_displacement=float(np.max(abs(x['prices']/x['prices'][0]-1))),
        completed_holding_minutes=STATS([(e['close_tick']-e['entry_tick'])/12 for e in done]),
        unfinished=summary['behavior']['unfinished'],short_realized_per_account=float(summary['behavior']['realized_pnl'])/5)


def check(x,s):
    assert not s['events'] and not x['injection_gold'].any() and not x['injection_foreign'].any()
    assert all(np.isfinite(v).all() for v in x.values())
    assert x['account_gold'].min()>=0 and x['account_foreign'].min()>=0
    np.testing.assert_allclose(x['gold']+x['account_gold'].sum(axis=1),1100000,atol=1e-8,rtol=0)
    np.testing.assert_allclose(x['pool_foreign']+x['account_foreign'].sum(axis=1),220000,atol=1e-8,rtol=0)
    np.testing.assert_allclose(x['account_wealth'],x['account_gold']+x['account_foreign']*x['prices'][:,None],atol=1e-8,rtol=0)
    np.testing.assert_array_equal(x['account_trades'].sum(axis=1),x['trades'])


def run_case(frozen,name,seed,a,t):
    cfg,profiles,b,trend=setup(frozen,a,t)
    cfg['seeds']=[seed]
    folder=OUT/name/f'seed{seed}';folder.mkdir(parents=True,exist_ok=True)
    if (folder/'metrics.json').exists():
        return json.loads((folder/'metrics.json').read_text())
    print('Running',name,'seed',seed,flush=True)
    x,s,episodes,fills,_,_=simulate(cfg,profiles,5,b,seed,{},frozen['policy'],{'trend_multiplier':trend})
    check(x,s)
    save_trace(folder,'trace',x,episodes,fills)
    (folder/'summary.json').write_text(json.dumps(clean_json(s),indent=2)+'\n')
    (folder/'parameters.json').write_text(json.dumps(dict(config=cfg,profiles=profiles,activity_b=b,trend_multiplier=trend),indent=2)+'\n')
    result=analyze(x,episodes,s)
    (folder/'metrics.json').write_text(json.dumps(clean_json(result),indent=2)+'\n')
    return result


def compare(baseline,candidate):
    b=baseline['swings']['0.005'];c=candidate['swings']['0.01']
    ratios=dict(wave_amplitude=c['amplitude']['median']/b['amplitude']['median'],
        wave_duration=c['cycles_minutes']['median']/b['cycles_minutes']['median'],
        single_leg_duration=c['legs_minutes']['median']/b['legs_minutes']['median'],
        matched_horizon_range=candidate['range60']['median']/baseline['range30']['median'])
    score=float(np.mean([abs(math.log(ratios[k]/2)) for k in ['wave_amplitude','wave_duration','matched_horizon_range']]))
    return dict(**ratios,score=score,clipped_fraction=candidate['clipped_fraction'],
        executed_quantity_fraction=candidate['executed_quantity_fraction'],
        cash_saturated_arrivals=candidate['cash_saturated_arrivals'],foreign_saturated_arrivals=candidate['foreign_saturated_arrivals'],
        trades_per_minute=candidate['trades_per_minute'],max_abs_displacement=candidate['max_abs_displacement'])


def average(rows):
    return {key:float(np.mean([r[key] for r in rows])) for key in rows[0]}


def main():
    frozen=json.loads(FROZEN_PATH.read_text())
    files=[FROZEN_PATH,ROOT/'research/fx/reversion/repeated/entry_model.py',
        ROOT/'research/fx/activity/model.py',ROOT/'research/fx/episodes/model.py',
        ROOT/'research/fx/reversion/inventory.py',ROOT/'research/fx/factor_activity/model.py']
    hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    manifest=dict(train_seeds=TRAIN,holdout_seeds=HOLDOUT,candidates=CANDIDATES,
        target_amplitude_ratio=2,target_wave_duration_ratio=2,tick_seconds=5,ticks=10000,
        training_score='Mean absolute log-ratio error to 2 for median leg amplitude, cycle duration, 30m->60m range; average seeds.',
        selection='Top two training candidates by score; holdout seeds never used for further parameter changes.',
        primary_sample='ticks 360..10000, 5s closes; initial 30m excluded for all configs.',
        caveats='No stationarity claim; longer reference/heat windows warm up during sample. Wallet clipping is reported, not hidden.',
        protected_sha256=hashes)
    (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    training={};baselines={}
    for seed in TRAIN:baselines[seed]=run_case(frozen,'baseline',seed,1,1)
    for name,(a,t) in CANDIDATES.items():
        training[name]=average([compare(baselines[seed],run_case(frozen,name,seed,a,t)) for seed in TRAIN])
        print('Training',name,json.dumps(training[name]),flush=True)
    ranked=sorted(training,key=lambda name:training[name]['score']);selected=ranked[:2]
    (OUT/'training.json').write_text(json.dumps(training,indent=2)+'\n')
    (OUT/'selected-before-holdout.json').write_text(json.dumps(dict(selected=selected,ranked=ranked),indent=2)+'\n')
    print('Selected before holdout:',selected,flush=True)
    holdout={name:[] for name in selected};raw_holdout={name:[] for name in selected}
    for seed in HOLDOUT:
        baseline=run_case(frozen,'baseline',seed,1,1)
        for name in selected:
            a,t=CANDIDATES[name];candidate=run_case(frozen,name,seed,a,t)
            holdout[name].append(dict(seed=seed,**compare(baseline,candidate)))
            raw_holdout[name].append(candidate)
    summaries={name:average([{k:v for k,v in r.items() if k!='seed'} for r in rows]) for name,rows in holdout.items()}
    result=dict(training=training,selected=selected,holdout=holdout,holdout_averages=summaries)
    (OUT/'results.json').write_text(json.dumps(result,indent=2)+'\n')
    rows=[dict(stage=stage,variant=name,**row) for stage,sets in [('training',training),('holdout',summaries)] for name,row in sets.items()]
    with (OUT/'comparison.csv').open('w') as f:
        w=csv.DictWriter(f,fieldnames=rows[0]);w.writeheader();w.writerows(rows)
    plot(selected,holdout)
    for name in selected:
        a,t=CANDIDATES[name];cfg,profiles,b,trend=setup(frozen,a,t)
        (OUT/f'candidate-{name}.json').write_text(json.dumps(dict(name=name,status='experimental_not_applied',
            tick_seconds=5,parameters=cfg,profiles=profiles,activity_b=b,trend_multiplier=trend),indent=2)+'\n')
    for path,digest in hashes.items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest,path
    (OUT/'audit.json').write_text(json.dumps(dict(paths=23,all_paths_conserved=True,wallets_nonnegative=True,
        wallet_values_verified=True,trade_counts_verified=True,frozen_sources_unchanged=True,
        selected_before_holdout=True),indent=2)+'\n')
    print('Holdout averages:',json.dumps(summaries),flush=True)


def plot(selected,holdout):
    plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False,'font.size':10})
    colors=['#176997','#b27a16','#a14870'];names=['baseline',*selected]
    fig,axes=plt.subplots(3,2,figsize=(15,11),layout='constrained')
    for i,seed in enumerate(HOLDOUT):
        series={}
        for name in names:
            with np.load(OUT/name/f'seed{seed}'/'trace.npz') as z:series[name]=z['prices'].copy()
        lo=min(((p/p[0]-1)*100).min() for p in series.values())-.3
        hi=max(((p/p[0]-1)*100).max() for p in series.values())+.3
        for j,(name,p) in enumerate(series.items()):
            label='原配置' if name=='baseline' else name
            axes[i,0].plot(np.arange(len(p))/720,(p/p[0]-1)*100,color=colors[j],lw=.7,label=label)
            axes[i,1].plot(np.arange(1001),(p[:1001]/p[0]-1)*100,color=colors[j],lw=1,label=label)
        axes[i,0].set(title=f'验证seed {seed}：完整路径',xlabel='小时',ylabel='相对初价变化（%）',ylim=(lo,hi))
        axes[i,1].set(title=f'验证seed {seed}：前1000tick',xlabel='tick（5秒）',ylabel='相对初价变化（%）',ylim=(lo,hi))
    for ax in axes.flat:ax.grid(alpha=.18);ax.legend(fontsize=8);ax.axhline(0,color='#555',lw=.6)
    fig.suptitle('幅度×2、往返时长×2：未参与选参的三个种子，无外部冲击',fontsize=14)
    fig.savefig(OUT/'holdout-paths.png',dpi=150);plt.close(fig)
    fig,axes=plt.subplots(1,3,figsize=(14,4.5),layout='constrained')
    for ax,key,title in zip(axes,['wave_amplitude','wave_duration','matched_horizon_range'],
                           ['同等级波段幅度比（1%门槛／原0.5%）','同等级往返时长比','匹配时间窗口波幅比（60分／原30分）']):
        for j,name in enumerate(selected):
            values=[row[key] for row in holdout[name]]
            ax.scatter(j+np.array([-.1,0,.1]),values,color=colors[j+1],s=35)
            ax.plot([j-.2,j+.2],[np.mean(values)]*2,color=colors[j+1],lw=2)
        ax.axhline(2,color='#444',ls='--',label='目标2倍');ax.set(title=title,ylabel='相对原配置的倍数',xticks=range(2),xticklabels=selected,ylim=(0,None));ax.grid(axis='y',alpha=.18)
    fig.savefig(OUT/'holdout-ratios.png',dpi=150);plt.close(fig)


if __name__=='__main__':main()
