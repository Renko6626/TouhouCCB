"""Only stretch time on the 10/3/3/4 research composition; no size reduction.

Run: python output/fx-role-time-20261016/run.py
"""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import hashlib
import json
import runpy
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from research.fx.episodes.run import save_trace
from research.fx.factor_activity.run import clean_json

OUT=Path(__file__).resolve().parent
SOURCE=ROOT/'output/fx-role-mix-20261016'
HELPERS=runpy.run_path(str(SOURCE/'run.py'))
SIM,CHECK,METRICS=HELPERS['SIM'],HELPERS['CHECK'],HELPERS['metrics']
SEEDS=[20261029,20261030,20261031]
FACTOR=1.25


def write(path,value):
    path.write_text(json.dumps(clean_json(value),ensure_ascii=False,indent=2)+'\n')


def main():
    source=json.loads((SOURCE/'manifest.json').read_text())
    original=source['config']
    cfg=dict(original)
    for key in ['trend_window','mean_window','inventory_window']:
        cfg[key]=round(cfg[key]*FACTOR)
    cfg['tau']*=FACTOR
    b=source['activity_b']/FACTOR
    # Exact 1/FACTOR intensity at a given activity z, including background.
    cfg['mean_arrivals']=(1-source['activity_b'])*original['mean_arrivals']/FACTOR/(1-b)
    assert cfg['order_sigma_foreign']==108 and cfg['entry_quantity']==180
    assert cfg['inventory_position_cap']==450
    profiles=source['candidate_profiles']
    manifest=dict(seeds=SEEDS,factor=FACTOR,tick_seconds=5,config=cfg,activity_b=b,
        profiles=profiles,trend_multiplier=source['trend_multiplier'],groups=source['groups']['candidate'],
        differences={k:{'before':original[k],'after':v} for k,v in cfg.items() if v!=original[k]},
        activity_b_before=source['activity_b'],
        unchanged=['order sizes','inventory target cap and sensitivity','wallets','pool',
                   'signal coefficients and signal noise','take profit, stop loss, trailing exits','psi','tick seconds'],
        caveats=['Three exploratory seeds, no players, shocks or fees.',
                 'Window and arrival changes interact with endogenous activity; realized time scales need measurement.',
                 'Completed-holding statistics omit unfinished episodes; first 30 minutes excluded from main window metrics.'],
        source_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [SOURCE/'manifest.json',SOURCE/'model.py',SOURCE/'run.py']})
    write(OUT/'manifest.json',manifest)
    results={'current':{},'slower125':{}};traces={}
    previous=json.loads((SOURCE/'metrics.json').read_text())['candidate']
    for seed in SEEDS:
        with np.load(SOURCE/f'seed{seed}/trace.npz') as z: baseline={k:z[k] for k in z.files}
        results['current'][str(seed)]=previous[str(seed)]
        print('Running time-only 1.25x',seed,flush=True)
        x,s,e,f,inv,d=SIM({**cfg,'seeds':[seed]},profiles,3,b,seed,{},'weak_trend',
            {'trend_multiplier':source['trend_multiplier'],'episode_start':13})
        CHECK(x,s,cfg)
        folder=OUT/f'seed{seed}';folder.mkdir(exist_ok=True)
        save_trace(folder,'trace',x,e,f)
        write(folder/'summary.json',s);write(folder/'decisions.json',d)
        results['slower125'][str(seed)]=METRICS(x,e,s,'candidate',d)
        traces[seed]={'current':baseline,'slower125':x}
        print('Finished',seed,flush=True)
    write(OUT/'metrics.json',results)
    keys=['range30','range60','trades_per_minute','efficiency5_median','completed_holding_minutes']
    averages={}
    for name,rows in results.items():
        averages[name]={k:float(np.mean([m[k]['median'] if isinstance(m[k],dict) else m[k] for m in rows.values()])) for k in keys}
        for threshold in ['0.005','0.01']:
            for field in ['amplitude','cycles_minutes']:
                averages[name][f'swing{threshold}_{field}']=float(np.mean([m['swings'][threshold][field]['median'] for m in rows.values()]))
        averages[name]['mean_intensity']=float(np.mean([traces[s][name]['intensity'][361:].mean() for s in SEEDS]))
        averages[name]['gross_foreign_per_minute']=float(np.mean([traces[s][name]['executed_abs_quantity'][361:].sum()/(9640/12) for s in SEEDS]))
    write(OUT/'averages.json',averages)
    plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False,'font.size':10})
    for kind,start,end in [('1000ticks',360,1361),('full',0,10001)]:
        fig,axes=plt.subplots(3,2,figsize=(14,10),layout='constrained',sharex=True)
        for row,seed in enumerate(SEEDS):
            ps=[traces[seed][name]['prices'][start:end] for name in results]
            low,high=min(p.min() for p in ps),max(p.max() for p in ps);pad=(high-low)*.08
            for col,(name,label,color) in enumerate([('current','当前10／3／3／4组合','#176997'),
                                                   ('slower125','仅时间拉长1.25倍，订单规模不变','#b27a16')]):
                ax=axes[row,col];x=traces[seed][name];t=np.arange(start,end)
                ref=x['slow_reference'][start:end].copy()
                if start==0:ref[0]=np.nan
                ax.plot(t,x['prices'][start:end],lw=.9,color=color,label='池价')
                ax.plot(t,ref,lw=.8,color='#555555',ls='--',label='库存参考价')
                ax.set(title=f'{label}\nseed {seed}',xlabel='tick（5秒）',ylabel='金币／外币',
                       xlim=(start,end-1),ylim=(low-pad,high+pad))
                ax.grid(alpha=.18);ax.legend(ncol=2,fontsize=8)
        fig.suptitle(('热身后1000tick，第360～1360tick' if kind=='1000ticks' else '完整10000tick')+
                     '；每行坐标一致；浅池50万／10万；随机σ108；无冲击、零费率',fontsize=12)
        fig.savefig(OUT/f'comparison-{kind}.png',dpi=145);plt.close(fig)
    unchanged=all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in manifest['source_sha256'].items())
    assert unchanged
    write(OUT/'audit.json',dict(new_paths=3,reused_paths=3,finite_series=True,assets_conserved=True,
        nonnegative_wallets=True,wealth_and_trade_counts_verified=True,sources_unchanged=unchanged,
        sizes_unchanged=True,signal_parameters_unchanged=True))
    print(json.dumps(averages,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
