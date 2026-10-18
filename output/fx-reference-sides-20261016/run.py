"""Frozen-baseline reference-side diagnostics across six seeds; no shocks.

Run from repository root: python output/fx-reference-sides-20261016/run.py
"""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import csv
import hashlib
import json
import time
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from research.fx.reversion.repeated.entry_model import simulate
from research.fx.episodes.run import save_trace
from research.fx.factor_activity.run import clean_json

OUT=Path(__file__).resolve().parent
SEEDS=list(range(20261016,20261022))
FROZEN=ROOT/'research/fx/reversion/repeated/FROZEN_CONFIG.json'


def run_lengths(sign):
    edges=np.r_[0,np.flatnonzero(np.diff(sign)!=0)+1,len(sign)]
    return [(int(sign[a]),int(b-a)) for a,b in zip(edges[:-1],edges[1:])]


def describe(gap):
    upper=float(np.mean(gap>0));lower=float(np.mean(gap<0))
    runs=[length/12 for side,length in run_lengths(np.sign(gap)) if side]
    blocks=[]
    for width in [360,720]:
        n=len(gap)//width;parts=gap[:n*width].reshape(n,width)
        above=np.mean(parts>0,axis=1);below=np.mean(parts<0,axis=1)
        dominant=np.maximum(above,below)
        blocks.append(dict(minutes=width/12,count=n,discarded_ticks=len(gap)-n*width,
            above_fractions=above.tolist(),dominant_fraction_median=float(np.median(dominant)),
            fraction_at_least_80pct_one_side=float(np.mean(dominant>=.8)),
            fraction_at_least_90pct_one_side=float(np.mean(dominant>=.9))))
    return dict(ticks=len(gap),above_fraction=upper,below_fraction=lower,
        equal_fraction=float(np.mean(gap==0)),dominant_fraction=max(upper,lower),
        mean_relative_gap=float(np.mean(gap)),median_abs_relative_gap=float(np.median(abs(gap))),
        fraction_above_01pct=float(np.mean(gap>.001)),fraction_below_minus01pct=float(np.mean(gap<-.001)),
        longest_strict_side_minutes=max(runs,default=0),median_strict_side_minutes=float(np.median(runs)) if runs else None,
        strict_crossings=int(np.count_nonzero(np.sign(gap[1:])*np.sign(gap[:-1])<0)),
        runs_boundary_note='First and final runs are observed durations, possibly boundary-truncated.',
        blocks=blocks)


def main():
    frozen=json.loads(FROZEN.read_text())
    cfg={**frozen['research_reproduction']['selected_simulator_config'],**frozen['parameters']}
    assert cfg['ticks']==10000 and frozen['tick_seconds']==5
    sources=[FROZEN,ROOT/'research/fx/reversion/repeated/entry_model.py',
        ROOT/'research/fx/reversion/inventory.py',ROOT/'research/fx/episodes/model.py',
        ROOT/'research/fx/activity/model.py',ROOT/'research/fx/factor_activity/model.py']
    hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    manifest=dict(seeds=SEEDS,config=cfg,profiles=frozen['profiles'],policy=frozen['policy'],
        trend_multiplier=frozen['trend_multiplier'],activity_b=frozen['activity_b'],tick_seconds=5,shocks={},
        primary_sample='ticks 361..10000, after the first full 360-tick reference window',
        alignment='close[t] / slow_reference[t] - 1; also inspect observed pre-tick close[t-1]',
        block_rule='Non-overlapping blocks start at tick 361; >=80% time strictly on one side.',
        source_sha256=hashes)
    (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    results={};traces={};audits={};rows=[]
    for seed in SEEDS:
        start=time.monotonic();print('Running baseline seed',seed,flush=True)
        x,s,episodes,fills,_,_=simulate({**cfg,'seeds':[seed]},frozen['profiles'],5,frozen['activity_b'],seed,{},
            frozen['policy'],{'trend_multiplier':frozen['trend_multiplier']})
        assert not s['events'] and not np.any(x['injection_gold']) and not np.any(x['injection_foreign'])
        assert all(np.isfinite(a).all() for a in x.values())
        assert x['account_gold'].min()>=0 and x['account_foreign'].min()>=0
        np.testing.assert_allclose(x['gold']+x['account_gold'].sum(axis=1),1100000,atol=1e-8,rtol=0)
        np.testing.assert_allclose(x['pool_foreign']+x['account_foreign'].sum(axis=1),220000,atol=1e-8,rtol=0)
        np.testing.assert_array_equal(x['account_trades'].sum(axis=1),x['trades'])
        if seed==SEEDS[0]:
            with np.load(ROOT/'output/fx-timescale-20261016/trace.npz') as old:
                for key in old.files:np.testing.assert_array_equal(x[key],old[key],err_msg=key)
        logs=np.log(x['prices'])
        reference=np.exp(np.array([logs[max(0,t-360):t].mean() for t in range(1,10001)]))
        np.testing.assert_allclose(reference,x['slow_reference'][1:],rtol=1e-14,atol=0)
        gap=x['prices'][1:]/reference-1
        pre_gap=x['prices'][:-1]/reference-1
        d=dict(seed=seed,full=describe(gap),first1000=describe(gap[:1000]),
            mature=describe(gap[360:]),mature_pre_trade=describe(pre_gap[360:]),
            net_price_change=float(x['prices'][-1]/x['prices'][0]-1))
        results[str(seed)]=d;traces[seed]=x
        directory=OUT/f'seed{seed}';directory.mkdir(exist_ok=True)
        save_trace(directory,'trace',x,episodes,fills)
        (directory/'summary.json').write_text(json.dumps(clean_json(s),indent=2)+'\n')
        (directory/'metrics.json').write_text(json.dumps(d,indent=2)+'\n')
        m=d['mature'];b=m['blocks'][0]
        rows.append(dict(seed=seed,first1000_above=d['first1000']['above_fraction'],
            mature_above=m['above_fraction'],mature_below=m['below_fraction'],
            mature_pre_trade_above=d['mature_pre_trade']['above_fraction'],
            halfhour_blocks=b['count'],halfhour_80pct_one_side=b['fraction_at_least_80pct_one_side'],
            halfhour_dominant_share_median=b['dominant_fraction_median'],
            longest_strict_side_minutes=m['longest_strict_side_minutes'],
            strict_crossings=m['strict_crossings'],median_abs_gap=m['median_abs_relative_gap'],
            net_price_change=d['net_price_change']))
        audits[str(seed)]=dict(no_shocks=True,finite_series=True,wallets_nonnegative=True,assets_conserved=True,
            trade_counts_verified=True,reference_independently_recomputed=True,elapsed_seconds=time.monotonic()-start)
        print('Complete',seed,json.dumps(rows[-1]),flush=True)
    with (OUT/'comparison.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=rows[0]);writer.writeheader();writer.writerows(rows)
    (OUT/'metrics.json').write_text(json.dumps(results,indent=2)+'\n')
    plot(traces,results)
    for path,digest in hashes.items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest,path
    audits['frozen_sources_unchanged']=True;audits['original_seed_trace_exactly_reproduced']=True
    (OUT/'audit.json').write_text(json.dumps(audits,indent=2)+'\n')
    print('All six paths and checks complete.',flush=True)


def plot(traces,results):
    plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False,'font.size':10})
    fig,axes=plt.subplots(3,2,figsize=(15,11),sharex=True,sharey=True,layout='constrained')
    time=np.arange(361,10001)/720
    for ax,(seed,x) in zip(axes.flat,traces.items()):
        gap=(x['prices'][361:]/x['slow_reference'][361:]-1)*100
        m=results[str(seed)]['mature']
        ax.plot(time,gap,color='#176997',lw=.7)
        ax.fill_between(time,0,gap,where=gap>0,color='#b27a16',alpha=.3,interpolate=True)
        ax.fill_between(time,0,gap,where=gap<0,color='#176997',alpha=.2,interpolate=True)
        ax.axhline(0,color='#333',lw=.8)
        ax.set(title=f"seed {seed}：上方 {m['above_fraction']:.1%}，下方 {m['below_fraction']:.1%}",
            xlabel='模拟时间（小时）',ylabel='池价／库存参考价−1（%）',xlim=(time[0],time[-1]))
        ax.grid(alpha=.18)
    fig.suptitle('原配置，无冲击：扣除前30分钟后，价格在库存参考价的哪一侧？\n橙色为上方，蓝色为下方；六图坐标一致',fontsize=14)
    fig.savefig(OUT/'reference-gaps.png',dpi=150);plt.close(fig)
    fig,axes=plt.subplots(3,2,figsize=(15,11),sharex=True,layout='constrained')
    for ax,(seed,x) in zip(axes.flat,traces.items()):
        ax.plot(np.arange(1001),x['prices'][:1001],color='#176997',lw=1,label='池价')
        ax.plot(np.arange(1,1001),x['slow_reference'][1:1001],color='#555',lw=1,ls='--',label='库存参考价')
        m=results[str(seed)]['first1000']
        ax.set(title=f"seed {seed}：上方 {m['above_fraction']:.1%}，下方 {m['below_fraction']:.1%}",
            xlabel='tick（每tick为5秒）',ylabel='金圆券／外币',xlim=(0,1000))
        ax.grid(alpha=.18);ax.legend(fontsize=8)
    fig.suptitle('原配置，无冲击：六个种子的前1000tick（83分20秒）\n各图价格轴分别缩放，用于观察价格与参考价的关系',fontsize=14)
    fig.savefig(OUT/'first-1000-ticks.png',dpi=150);plt.close(fig)


if __name__=='__main__':main()
