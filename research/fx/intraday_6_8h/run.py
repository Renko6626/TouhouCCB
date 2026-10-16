"""Run the frozen intraday preset and summarize complete session prefixes.

python -m research.fx.intraday_6_8h.run --hours 8 --output output/fx-intraday-frozen-20261016
"""
import argparse
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from research.fx.episodes.run import save_trace
from research.fx.factor_activity.run import clean_json
from .model import simulate

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
SNAPSHOT=HERE/'FROZEN_CONFIG.json'


def write(path,value):
    path.write_text(json.dumps(clean_json(value),ensure_ascii=False,indent=2)+'\n')


def stats(values):
    a=np.asarray(values,dtype=float)
    return dict(n=len(a),median=float(np.median(a)),p10=float(np.quantile(a,.1)),p90=float(np.quantile(a,.9))) if len(a) else dict(n=0,median=None,p10=None,p90=None)


def swing_metrics(prices,threshold):
    """Retrospective alternating extrema, same boundary convention as prior study."""
    pivots=[];direction=0;low=high=extreme=0
    for i in range(1,len(prices)):
        if direction==0:
            if prices[i]<prices[low]:low=i
            if prices[i]>prices[high]:high=i
            if prices[i]>=prices[low]*(1+threshold):
                pivots.append((low,i));direction=1;extreme=i
            elif prices[i]<=prices[high]*(1-threshold):
                pivots.append((high,i));direction=-1;extreme=i
        elif direction==1:
            if prices[i]>prices[extreme]:extreme=i
            elif prices[i]<=prices[extreme]*(1-threshold):
                pivots.append((extreme,i));direction=-1;extreme=i
        else:
            if prices[i]<prices[extreme]:extreme=i
            elif prices[i]>=prices[extreme]*(1+threshold):
                pivots.append((extreme,i));direction=1;extreme=i
    legs=[dict(minutes=(b[0]-a[0])/12,change=float(prices[b[0]]/prices[a[0]]-1))
          for a,b in zip(pivots[1:-1],pivots[2:])]
    up=[v for v in legs if v['change']>0]
    return dict(legs_minutes=stats([v['minutes'] for v in legs]),
        cycles_minutes=stats([(pivots[i+2][0]-pivots[i][0])/12 for i in range(1,len(pivots)-2)]),
        amplitude=stats([abs(v['change']) for v in legs]),
        up_minutes=stats([v['minutes'] for v in up]),up_gain=stats([v['change'] for v in up]))


def summarize(x,episodes,hours):
    end=hours*720;start=360;p=x['prices'][start:end+1]
    blocks=[p[i:i+361] for i in range(0,len(p)-360,360)]
    done=[e for e in episodes if e['entry_tick']>=start and e['close_tick'] is not None and e['close_tick']<=end]
    pending=[e for e in episodes if e['entry_tick']<=end and (e['close_tick'] is None or e['close_tick']>end)]
    return dict(hours=hours,excluded_startup_minutes=30,
        total_trades=int(x['trades'][1:end+1].sum()),
        trades_per_minute_full=float(x['trades'][1:end+1].sum()/(hours*60)),
        trades_per_minute_after30=float(x['trades'][start+1:end+1].sum()/((end-start)/12)),
        arrivals_per_minute_after30=float(x['arrivals'][start+1:end+1].sum()/((end-start)/12)),
        range30=stats([(b.max()-b.min())/b[0] for b in blocks]),
        gross_foreign_per_minute_after30=float(x['executed_abs_quantity'][start+1:end+1].sum()/((end-start)/12)),
        net_change=float(x['prices'][end]/x['prices'][0]-1),
        max_abs_displacement=float(np.max(abs(x['prices'][:end+1]/x['prices'][0]-1))),
        completed_holding_minutes=stats([(e['close_tick']-e['entry_tick'])/12 for e in done]),
        unfinished_episodes=len(pending),
        unfinished_age_minutes=stats([(end-e['entry_tick'])/12 for e in pending]),
        swings={str(t):swing_metrics(p,t) for t in [.005,.01,.02]})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hours',type=int,choices=[6,7,8],default=7)
    parser.add_argument('--seeds',type=int,nargs='+',default=[20261029,20261030,20261031])
    parser.add_argument('--output',type=Path,default=ROOT/'output/fx-intraday-6-8h')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    frozen=json.loads(SNAPSHOT.read_text());cfg={**frozen['parameters'],'ticks':args.hours*720}
    assert cfg['mean_arrivals']==.6 and sum(frozen['roles'].values())==cfg['accounts']==20
    for rel,digest in frozen['provenance_sha256'].items():
        if rel.endswith('/model.py'):assert hashlib.sha256((ROOT/rel).read_bytes()).hexdigest()==digest
    sources=[SNAPSHOT,HERE/'model.py',HERE/'run.py',ROOT/'research/fx/activity/model.py',
             ROOT/'research/fx/episodes/model.py',ROOT/'research/fx/common/clock.py',
             ROOT/'research/fx/reversion/inventory.py',ROOT/'backend/app/services/fx/amm.py']
    hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    write(args.output/'manifest.json',dict(preset=frozen['name'],hours=args.hours,seeds=args.seeds,source_sha256=hashes,
        definition='Each prefix starts at the same cold start; no shocks, players, fees or forced terminal liquidation.'))
    results={};prefix_verified=[]
    for seed in args.seeds:
        print('Running frozen intraday',args.hours,'hours seed',seed,flush=True)
        x,s,e,f,_,_=simulate({**cfg,'seeds':[seed]},frozen['profiles'],3,frozen['activity_b'],seed,{},
            frozen['policy'],{'trend_multiplier':frozen['trend_multiplier'],'episode_start':13})
        assert all(np.isfinite(v).all() for v in x.values())
        assert x['account_gold'].min()>=0 and x['account_foreign'].min()>=0
        assert not s['events'] and not x['injection_gold'].any() and not x['injection_foreign'].any()
        np.testing.assert_allclose(x['gold']+x['account_gold'].sum(axis=1),600000,atol=1e-8,rtol=0)
        np.testing.assert_allclose(x['pool_foreign']+x['account_foreign'].sum(axis=1),120000,atol=1e-8,rtol=0)
        np.testing.assert_allclose(x['account_wealth'],x['account_gold']+x['account_foreign']*x['prices'][:,None],atol=1e-8,rtol=0)
        np.testing.assert_array_equal(x['account_trades'].sum(axis=1),x['trades'])
        old=ROOT/f'output/fx-arrivals-0.6-20261016/seed{seed}/trace.npz'
        if old.exists():
            with np.load(old) as z:
                for key in x:np.testing.assert_array_equal(x[key],z[key][:cfg['ticks']+1])
            prefix_verified.append(seed)
        folder=args.output/f'seed{seed}';folder.mkdir(exist_ok=True)
        save_trace(folder,'trace',x,e,f);write(folder/'summary.json',s)
        results[str(seed)]={str(h):summarize(x,e,h) for h in [6,7,8] if h<=args.hours}
    write(args.output/'metrics.json',results)
    averages={}
    for h in [6,7,8]:
        if h>args.hours:continue
        rows=[r[str(h)] for r in results.values()]
        avg={k:float(np.mean([r[k] for r in rows])) for k in ['total_trades','trades_per_minute_full','trades_per_minute_after30','arrivals_per_minute_after30','gross_foreign_per_minute_after30','net_change','unfinished_episodes']}
        avg['range30_median']=float(np.mean([r['range30']['median'] for r in rows]))
        avg['cycle1_minutes']=float(np.mean([r['swings']['0.01']['cycles_minutes']['median'] for r in rows]))
        avg['amplitude1']=float(np.mean([r['swings']['0.01']['amplitude']['median'] for r in rows]))
        avg['up1_minutes']=float(np.mean([r['swings']['0.01']['up_minutes']['median'] for r in rows]))
        avg['up1_gain']=float(np.mean([r['swings']['0.01']['up_gain']['median'] for r in rows]))
        averages[str(h)]=avg
    write(args.output/'averages.json',averages)
    assert all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==v for p,v in hashes.items())
    write(args.output/'audit.json',dict(paths=len(args.seeds),session_hours=args.hours,
        exact_prior_prefix_verified_seeds=prefix_verified,finite_series=True,assets_conserved=True,
        wallets_nonnegative=True,wealth_and_trade_counts_verified=True,no_shocks=True,source_hashes_unchanged=True))
    print(json.dumps(averages,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
