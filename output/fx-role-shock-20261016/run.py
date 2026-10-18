"""Natural wave scales and isolated +/-5% reserve-injection shock responses.

Run: python output/fx-role-shock-20261016/run.py
"""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import hashlib
import json
import math
import runpy
from collections import defaultdict
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from research.fx.episodes.run import save_trace
from research.fx.factor_activity.run import clean_json

OUT=Path(__file__).resolve().parent
SOURCE=ROOT/'output/fx-role-time-20261016'
SIM=runpy.run_path(str(ROOT/'output/fx-role-mix-20261016/model.py'))['simulate']
SWINGS=runpy.run_path(str(ROOT/'output/fx-timescale-20261016/run.py'))['swings']
SEEDS=[20261029,20261030,20261031]
EVENT=2000
FOLLOW=3600


def write(path,value):path.write_text(json.dumps(clean_json(value),ensure_ascii=False,indent=2)+'\n')


def describe(values):
    a=np.asarray(values,dtype=float)
    return dict(n=len(a),median=float(np.median(a)),p10=float(np.quantile(a,.1)),p90=float(np.quantile(a,.9))) if len(a) else dict(n=0,median=None,p10=None,p90=None)


def crossing(curve,level=.5):
    hits=np.flatnonzero(curve<=level)
    sustained=[i for i in range(len(curve)-23) if np.all(curve[i:i+24]<=level)]
    return dict(first_minutes=float(hits[0]/12) if len(hits) else None,
                sustained_2min_start_minutes=float(sustained[0]/12) if sustained else None,
                residual={str(m):float(curve[m*12]) for m in [0,5,15,30,60,120,180,240,300]})


def main():
    source=json.loads((SOURCE/'manifest.json').read_text())
    cfg={**source['config'],'ticks':EVENT+FOLLOW}
    baselines={}
    for seed in SEEDS:
        with np.load(SOURCE/f'seed{seed}/trace.npz') as z:baselines[seed]={k:z[k] for k in z.files}
    tau_min=cfg['tau']/12
    natural={};legs_all={};cycles_all={};choices=[]
    for threshold in [.005,.01,.02]:
        legs=[];cycles=[]
        for seed in SEEDS:
            pivots,ls,cs=SWINGS(baselines[seed]['prices'][360:],threshold)
            legs.extend(ls);cycles.extend(cs)
            if threshold==.01:
                for i in range(1,len(pivots)-2):
                    ticks=[pivots[j][0]+360 for j in [i,i+1,i+2]]
                    prices=baselines[seed]['prices'][ticks]
                    choices.append(dict(seed=seed,ticks=ticks,duration=(ticks[-1]-ticks[0])/12,
                        amplitudes=[abs(float(prices[j+1]/prices[j]-1)) for j in [0,1]]))
        natural[str(threshold)]=dict(legs_minutes=describe([l['minutes'] for l in legs]),
            cycles_minutes=describe(cycles),amplitude=describe([abs(l['change']) for l in legs]))
    target_time=natural['0.01']['cycles_minutes']['median']
    target_amp=natural['0.01']['amplitude']['median']
    typical=min(choices,key=lambda c:abs(math.log(c['duration']/target_time))+
                sum(abs(math.log(a/target_amp)) for a in c['amplitudes'])/2)
    write(OUT/'natural.json',dict(pooled_statistics=natural,representative_cycle=typical,
        selection='Nearest pooled median cycle duration and median leg amplitudes in log-ratio distance; all three seeds searched.',
        mechanics=dict(short_window_minutes=cfg['trend_window']/12,inventory_window_minutes=cfg['inventory_window']/12,
                       z_e_fold_minutes=tau_min,z_half_minutes=tau_min*math.log(2),
                       z_squared_half_minutes=tau_min*math.log(2)/2),
        definition='Swings confirmed by relative price reversals; durations use extremum times and are identified retrospectively. First 30min excluded.'))
    files=[SOURCE/'manifest.json',ROOT/'output/fx-role-mix-20261016/model.py']
    manifest=dict(config=cfg,activity_b=source['activity_b'],profiles=source['profiles'],
        trend_multiplier=source['trend_multiplier'],event_tick=EVENT,event_abs_return=.05,
        followup_minutes=FOLLOW/12,seeds=SEEDS,
        shock_mechanism='Positive shock injects gold; negative shock injects foreign, to shift marginal price by requested fraction; pool product changes.',
        paired_residual='(shock price - no-shock control price) / (immediate post-shock price - pre-shock price)',
        raw_residual='(shock price - immediate pre-shock price) / initial jump',
        caveats=['No players and zero fees; injection is not a player trade or fixed-product swap.',
                 'Same random streams; endogenous arrival paths diverge after shock. Paired response is descriptive over only 3 seeds per direction.',
                 'Half crossings can reverse later; sustained means 24 consecutive 5s closes, timestamped at first close.',
                 'No exponential price-response fit; baseline before shock matches exactly.'],
        source_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files})
    write(OUT/'manifest.json',manifest)
    records=[];curves=defaultdict(list)
    for seed in SEEDS:
        for jump in [.05,-.05]:
            label='up' if jump>0 else 'down'
            print('Running',seed,label,flush=True)
            x,s,episodes,fills,_,_=SIM({**cfg,'seeds':[seed]},source['profiles'],3,source['activity_b'],seed,
                {EVENT:jump},'weak_trend',{'trend_multiplier':source['trend_multiplier'],'episode_start':13})
            for key in x:np.testing.assert_array_equal(x[key][:EVENT],baselines[seed][key][:EVENT])
            assert all(np.isfinite(a).all() for a in x.values())
            assert x['account_gold'].min()>=0 and x['account_foreign'].min()>=0
            np.testing.assert_allclose(x['gold']+x['account_gold'].sum(axis=1),
                cfg['pool_gold']+20*cfg['initial_gold']+np.cumsum(x['injection_gold']),atol=1e-8,rtol=0)
            np.testing.assert_allclose(x['pool_foreign']+x['account_foreign'].sum(axis=1),
                cfg['pool_foreign']+20*cfg['initial_foreign']+np.cumsum(x['injection_foreign']),atol=1e-8,rtol=0)
            np.testing.assert_array_equal(x['account_trades'].sum(axis=1),x['trades'])
            event=s['events'][0];pre=float(event['pre_price']);post=float(event['post_price'])
            assert abs(post/pre-1-jump)<1e-10
            assert abs(pre-baselines[seed]['prices'][EVENT])<1e-12
            prices=x['prices'][EVENT:];control=baselines[seed]['prices'][EVENT:EVENT+FOLLOW+1]
            paired=(prices-control)/(post-pre);raw=(prices-pre)/(post-pre)
            record=dict(seed=seed,direction=label,paired=crossing(paired),raw=crossing(raw))
            records.append(record);curves[label].append(paired)
            folder=OUT/f'{label}-seed{seed}';folder.mkdir(exist_ok=True)
            save_trace(folder,'trace',x,episodes,fills);write(folder/'summary.json',s)
            print('Finished',seed,label,'paired half',record['paired']['first_minutes'],flush=True)
    aggregates={}
    for label,values in curves.items():
        values=np.array(values);mean=values.mean(axis=0)
        aggregates[label]=dict(mean_curve=crossing(mean),
            last_5min_mean_residual={str(m):float(values[:,m*12-59:m*12+1].mean()) for m in [15,30,60,120,240,300]},
            first_half_observed=sum(r['paired']['first_minutes'] is not None for r in records if r['direction']==label),
            sustained_half_observed=sum(r['paired']['sustained_2min_start_minutes'] is not None for r in records if r['direction']==label))
    write(OUT/'responses.json',dict(events=records,aggregates=aggregates))
    plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False,'font.size':10})
    fig,axes=plt.subplots(1,2,figsize=(13,4.5),layout='constrained',sharey=True)
    t=np.arange(FOLLOW+1)/12
    for ax,label,title,color in zip(axes,['up','down'],['上涨冲击＋5%','下跌冲击－5%'],['#176997','#b27a16']):
        for seed,curve in zip(SEEDS,curves[label]):ax.plot(t,curve*100,lw=.8,alpha=.45,color=color,label=f'seed {seed}')
        ax.plot(t,np.mean(curves[label],axis=0)*100,color=color,lw=2,label='三种子均值')
        ax.axhline(50,color='#555',ls='--',lw=.8,label='剩余50%');ax.axhline(0,color='#777',lw=.7)
        ax.set(title=title,xlabel='冲击后分钟',ylabel='相对无冲击对照的剩余偏离（初始冲击＝100%）',xlim=(0,300))
        ax.grid(alpha=.18);ax.legend(fontsize=8,ncol=2)
    fig.suptitle('10噪声＋3库存＋3回归＋4趋势，时间拉长1.25倍；一次储备注入冲击；无玩家、零费率')
    fig.savefig(OUT/'shock-response.png',dpi=150);plt.close(fig)
    p=baselines[typical['seed']]['prices'];a,mid,b=typical['ticks']
    lo=max(360,a-180);hi=min(len(p)-1,b+180)
    fig,ax=plt.subplots(figsize=(12,4.5),layout='constrained')
    ax.plot(np.arange(lo,hi+1)/12,p[lo:hi+1],color='#176997',lw=1)
    ax.plot(np.array([a,mid,b])/12,p[[a,mid,b]],'o-',color='#b27a16',lw=2,label='按中位时长与幅度选出的完整波段')
    for j,(start,end) in enumerate([(a,mid),(mid,b)]):
        ax.annotate(f'{(end-start)/12:.1f}分钟，{(p[end]/p[start]-1)*100:+.2f}%',
            ((start+end)/24,(p[start]+p[end])/2),xytext=(0,20 if j==0 else -28),textcoords='offset points',ha='center')
    ax.set(title=f'典型波段示例：seed {typical["seed"]}，1%反转门槛，完整往返{typical["duration"]:.1f}分钟',
           xlabel='模拟开始后分钟',ylabel='金币／外币',xlim=(lo/12,hi/12))
    ax.grid(alpha=.18);ax.legend(fontsize=9)
    fig.savefig(OUT/'typical-wave.png',dpi=150);plt.close(fig)
    unchanged=all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in manifest['source_sha256'].items())
    assert unchanged
    write(OUT/'audit.json',dict(new_shock_paths=6,reused_normal_paths=3,pre_shock_prefix_exact=True,
        jump_sizes_verified=True,finite_series=True,wallets_nonnegative=True,assets_conserved_with_injections=True,
        trade_counts_verified=True,sources_unchanged=True))
    print(json.dumps(dict(natural=natural,aggregates=aggregates,records=records),ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
