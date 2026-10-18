"""Background arrival-rate sweep; all other model parameters unchanged.

Run: python output/fx-arrival-rate-20261016/run.py
"""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import json
import hashlib
import runpy
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from research.fx.episodes.run import save_trace
from research.fx.factor_activity.run import clean_json

OUT=Path(__file__).resolve().parent
SOURCE=ROOT/'output/fx-role-time-20261016'
H=runpy.run_path(str(ROOT/'output/fx-role-mix-20261016/run.py'))
SIM,CHECK,METRICS=H['SIM'],H['CHECK'],H['metrics']
SEEDS=[20261029,20261030,20261031]
MULTIPLIERS=[2,2.5,3]


def write(path,value):path.write_text(json.dumps(clean_json(value),ensure_ascii=False,indent=2)+'\n')


def main():
    source=json.loads((SOURCE/'manifest.json').read_text())
    original=source['config']
    manifest=dict(seeds=SEEDS,multipliers=MULTIPLIERS,config=original,
        activity_b=source['activity_b'],profiles=source['profiles'],trend_multiplier=source['trend_multiplier'],
        changed_parameter='mean_arrivals only; activity_b, psi, tau and all other parameters unchanged',
        formula='intensity = (1-b)*mean_arrivals + b*z*z; mean_arrivals is not actual filled trades',
        count='Each successful order is one trade, measured after tick360; arrivals and skipped decisions are separate.',
        scope='Offline exploration only; no production configuration writes, no players, shocks or fees.',
        source_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [SOURCE/'manifest.json',ROOT/'output/fx-role-mix-20261016/model.py']})
    write(OUT/'manifest.json',manifest)
    results={'current':json.loads((SOURCE/'metrics.json').read_text())['slower125']}
    traces={}
    for seed in SEEDS:
        with np.load(SOURCE/f'seed{seed}/trace.npz') as z:traces[('current',seed)]={k:z[k] for k in z.files}
    for multiplier in MULTIPLIERS:
        name=f'rate{multiplier:g}';results[name]={}
        for seed in SEEDS:
            cfg={**original,'seeds':[seed],'mean_arrivals':original['mean_arrivals']*multiplier}
            assert {k for k in original if k!='seeds' and cfg[k]!=original[k]}=={'mean_arrivals'}
            print('Running',name,seed,flush=True)
            x,s,e,f,_,d=SIM(cfg,source['profiles'],3,source['activity_b'],seed,{},'weak_trend',
                {'trend_multiplier':source['trend_multiplier'],'episode_start':13})
            CHECK(x,s,cfg)
            folder=OUT/name/f'seed{seed}';folder.mkdir(parents=True,exist_ok=True)
            save_trace(folder,'trace',x,e,f);write(folder/'summary.json',s)
            write(folder/'parameters.json',cfg)
            m=METRICS(x,e,s,'candidate',d)
            minutes=9640/12
            m.update(arrivals_per_minute=float(x['arrivals'][361:].sum()/minutes),
                gross_foreign_per_minute=float(x['executed_abs_quantity'][361:].sum()/minutes),
                mean_intensity=float(x['intensity'][361:].mean()),peak_intensity=float(x['intensity'][361:].max()))
            # Fixed, non-overlapping 5-minute counts capture burstiness without
            # conflating average rates with guarantees in every minute.
            counts=x['trades'][361:361+(9640//60)*60].reshape(-1,60).sum(axis=1)/5
            m['five_minute_rate']={'median':float(np.median(counts)),'p10':float(np.quantile(counts,.1)),
                                   'p90':float(np.quantile(counts,.9))}
            results[name][str(seed)]=m;traces[(name,seed)]=x
            print('Finished',name,seed,'trades/min',round(m['trades_per_minute'],3),flush=True)
    averages={}
    for name,rows in results.items():
        averages[name]={k:float(np.mean([r[k]['median'] if isinstance(r[k],dict) else r[k] for r in rows.values()]))
            for k in ['trades_per_minute','range30','range60','efficiency5_median','completed_holding_minutes']}
        for key,field in [('cycle1_minutes','cycles_minutes'),('amplitude1','amplitude')]:
            averages[name][key]=float(np.mean([r['swings']['0.01'][field]['median'] for r in rows.values()]))
        averages[name]['gross_foreign_per_minute']=float(np.mean([traces[(name,s)]['executed_abs_quantity'][361:].sum()/(9640/12) for s in SEEDS]))
        averages[name]['per_seed_trades_per_minute']=[rows[str(s)]['trades_per_minute'] for s in SEEDS]
    # Pick the lowest tested background rate with all three long-run rates in target.
    eligible=[f'rate{v:g}' for v in MULTIPLIERS
        if all(6<=results[f'rate{v:g}'][str(s)]['trades_per_minute']<=10 for s in SEEDS)]
    selected=eligible[0] if eligible else None
    write(OUT/'metrics.json',results);write(OUT/'averages.json',averages)
    write(OUT/'selection.json',dict(selected=selected,eligible=eligible,
        rule='Lowest tested rate with every exploratory seed average between6 and10; no independent holdout.'))
    if selected:
        plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False,'font.size':10})
        fig,axes=plt.subplots(3,2,figsize=(14,10),sharex=True,layout='constrained')
        for row,seed in enumerate(SEEDS):
            pairs=[traces[(name,seed)] for name in ['current',selected]]
            low=min(x['prices'][360:1361].min() for x in pairs);high=max(x['prices'][360:1361].max() for x in pairs)
            pad=(high-low)*.08
            for col,(name,color) in enumerate([('current','#176997'),(selected,'#b27a16')]):
                x=traces[(name,seed)];ax=axes[row,col]
                ax.plot(np.arange(360,1361),x['prices'][360:1361],color=color,lw=.9,label='池价')
                ax.plot(np.arange(360,1361),x['slow_reference'][360:1361],color='#555555',ls='--',lw=.8,label='库存参考价')
                rate=results[name][str(seed)]['trades_per_minute']
                label='当前配置' if name=='current' else f'背景到达率×{name[4:]}'
                ax.set(title=f'{label}：全程热身后平均{rate:.2f}笔／分钟\nseed {seed}',
                       xlabel='tick（5秒）',ylabel='金币／外币',xlim=(360,1360),ylim=(low-pad,high+pad))
                ax.grid(alpha=.18);ax.legend(ncol=2,fontsize=8)
        fig.suptitle('仅调整背景到达率；其他参数不变；10噪声＋3库存＋3回归＋4趋势；无玩家、无冲击、零费率')
        fig.savefig(OUT/'comparison-1000ticks.png',dpi=145);plt.close(fig)
    assert all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in manifest['source_sha256'].items())
    write(OUT/'audit.json',dict(new_paths=9,reused_paths=3,only_background_arrival_parameter_changed=True,
        finite_series=True,nonnegative_wallets=True,assets_conserved=True,wealth_and_trade_counts_verified=True,
        source_hashes_unchanged=True))
    print(json.dumps(dict(averages=averages,selected=selected),ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
