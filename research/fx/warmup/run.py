"""python -m research.fx.warmup.run --hours 24 --output output/fx-warmup-24h-20261016"""
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np
from research.fx.intraday_6_8h.model import simulate
from research.fx.intraday_6_8h.run import summarize,swing_metrics
from research.fx.episodes.run import save_trace
from research.fx.factor_activity.run import clean_json

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]


def write(path,value):path.write_text(json.dumps(clean_json(value),ensure_ascii=False,indent=2)+'\n')


def metrics(x,e,hours):
    m=summarize(x,e,hours)
    m['matched_waves']=swing_metrics(x['prices'][360:],.015)
    for start_hours in [6,12]:
        if hours>start_hours:
            start=start_hours*720
            m[f'after{start_hours}h']=dict(trades_per_minute=float(x['trades'][start+1:].sum()/((len(x['prices'])-1-start)/12)),
                waves=swing_metrics(x['prices'][start:],.015))
    attempts=x['order_attempts'][361:].sum()
    m['clipped_fraction']=float(x['clipped_orders'][361:].sum()/attempts)
    m['cash_saturated_arrivals']=int(x['cash_saturated'][361:].sum())
    m['foreign_saturated_arrivals']=int(x['foreign_saturated'][361:].sum())
    return m


def compact(rows):
    def avg(fn):return float(np.mean([fn(r) for r in rows]))
    return dict(trades_per_minute=avg(lambda r:r['trades_per_minute_after30']),
        cycle_minutes=avg(lambda r:r['matched_waves']['cycles_minutes']['median']),
        up_minutes=avg(lambda r:r['matched_waves']['up_minutes']['median']),
        up_gain=avg(lambda r:r['matched_waves']['up_gain']['median']),
        fixed1pct_cycle_minutes=avg(lambda r:r['swings']['0.01']['cycles_minutes']['median']),
        clipped_fraction=avg(lambda r:r['clipped_fraction']),
        per_seed=[dict(trades=r['trades_per_minute_after30'],cycle=r['matched_waves']['cycles_minutes']['median'],
                       up=r['matched_waves']['up_minutes']['median'],gain=r['matched_waves']['up_gain']['median']) for r in rows])


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--hours',type=int,default=24)
    p.add_argument('--seeds',type=int,nargs='+',default=[20261029,20261030,20261031,20261102])
    p.add_argument('--output',type=Path,default=ROOT/'output/fx-warmup-24h')
    args=p.parse_args()
    if not 6<=args.hours<=168:p.error('--hours must be between6 and168; longer than24h has not yet been validated')
    args.output.mkdir(parents=True,exist_ok=True)
    snapshot=HERE/'CONFIG.json';f=json.loads(snapshot.read_text())
    day=ROOT/f['calibration']['original_day_frozen_config'];day_hash=f['calibration']['day_config_sha256']
    assert hashlib.sha256(day.read_bytes()).hexdigest()==day_hash
    cfg={**f['parameters'],'ticks':args.hours*720}
    files=[snapshot,HERE/'run.py',ROOT/'research/fx/intraday_6_8h/model.py',ROOT/'research/fx/intraday_6_8h/run.py',
           ROOT/'research/fx/episodes/model.py',ROOT/'research/fx/activity/model.py',ROOT/'backend/app/services/fx/amm.py']
    hashes={str(v.relative_to(ROOT)):hashlib.sha256(v.read_bytes()).hexdigest() for v in files}
    write(args.output/'manifest.json',dict(preset=f['name'],hours=args.hours,seeds=args.seeds,source_sha256=hashes,
        primary_wave_reversal=.015,fixed_wave_reversal=.01,selection_seeds=[20261029,20261030,20261031],
        additional_seeds_selected_before_check=[s for s in args.seeds if s not in [20261029,20261030,20261031]],source_day_sha256=day_hash))
    results={};prefix_verified=[]
    for seed in args.seeds:
        print('Running warmup',args.hours,'hours',seed,flush=True)
        x,s,e,fills,_,_=simulate({**cfg,'seeds':[seed]},f['profiles'],3,f['activity_b'],seed,{},f['policy'],
            {'trend_multiplier':f['trend_multiplier'],'episode_start':13})
        assert all(np.isfinite(v).all() for v in x.values())
        assert x['account_gold'].min()>=0 and x['account_foreign'].min()>=0
        assert not s['events'] and not x['injection_gold'].any() and not x['injection_foreign'].any()
        np.testing.assert_allclose(x['gold']+x['account_gold'].sum(axis=1),600000,atol=1e-8,rtol=0)
        np.testing.assert_allclose(x['pool_foreign']+x['account_foreign'].sum(axis=1),120000,atol=1e-8,rtol=0)
        np.testing.assert_allclose(x['account_wealth'],x['account_gold']+x['account_foreign']*x['prices'][:,None],atol=1e-8,rtol=0)
        np.testing.assert_array_equal(x['account_trades'].sum(axis=1),x['trades'])
        choice=f['calibration']['source_choice']
        old=ROOT/f'output/fx-warmup-calibration-20261016/{choice}/12h-seed{seed}/trace.npz'
        if args.hours>=12 and old.exists():
            with np.load(old) as z:
                for key in x:np.testing.assert_array_equal(x[key][:8641],z[key])
            prefix_verified.append(seed)
        folder=args.output/f'seed{seed}';folder.mkdir(exist_ok=True)
        save_trace(folder,'trace',x,e,fills);write(folder/'summary.json',s)
        results[str(seed)]=metrics(x,e,args.hours)
        write(folder/'metrics.json',results[str(seed)])
        print('Finished',seed,compact([results[str(seed)]]),flush=True)
    write(args.output/'metrics.json',results)
    aggregate=dict(all_seeds=compact(list(results.values())),
        original_selection_seeds=compact([r for s,r in results.items() if int(s) in [20261029,20261030,20261031]]))
    extra=[r for s,r in results.items() if int(s) not in [20261029,20261030,20261031]]
    if extra:aggregate['additional_seeds']=compact(extra)
    write(args.output/'averages.json',aggregate)
    assert hashlib.sha256(day.read_bytes()).hexdigest()==day_hash
    assert all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in hashes.items())
    write(args.output/'audit.json',dict(paths=len(results),hours=args.hours,source_hashes_unchanged=True,
        day_frozen_config_unchanged=True,assets_conserved=True,wallets_nonnegative=True,finite_series=True,
        wealth_and_trade_counts_verified=True,exact_12h_prefix_verified_seeds=prefix_verified))
    print(json.dumps(aggregate,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
