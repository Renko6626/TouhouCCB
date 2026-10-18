"""Warmup preset: 40-60min complete cycles, ~50% larger directional amplitude.

python output/fx-warmup-calibration-20261016/run.py
"""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import hashlib
import json
import math
import numpy as np
from research.fx.intraday_6_8h.model import simulate
from research.fx.intraday_6_8h.run import summarize,swing_metrics
from research.fx.episodes.run import save_trace
from research.fx.factor_activity.run import clean_json

OUT=Path(__file__).resolve().parent
SOURCE=ROOT/'output/fx-cycle20-20261016/candidate.json'
DAY=ROOT/'research/fx/intraday_6_8h/FROZEN_CONFIG.json'
SEEDS=[20261029,20261030,20261031]
HOURS=12
CHOICES={
    'time2':dict(time=2,arrival=.75,slow_inventory=True),
    'time25':dict(time=2.5,arrival=.75,slow_inventory=True),
    'time3':dict(time=3,arrival=.75,slow_inventory=True),
    'time25_fast_inventory':dict(time=2.5,arrival=.75,slow_inventory=False),
    # Follow-up:1.5x nominal scaling produced only2.3-2.5% up legs in finite
    # wallets. Test modest additional scaling against the same1.5% wave gate.
    'time3_amp17':dict(time=3,arrival=.75,slow_inventory=True,amplitude=1.7),
    'time3_amp17_fast_inventory':dict(time=3,arrival=.75,slow_inventory=False,amplitude=1.7),
    'time35_amp17_fast_inventory':dict(time=3.5,arrival=.75,slow_inventory=False,amplitude=1.7),
    'time4_amp19':dict(time=4,arrival=.75,slow_inventory=True,amplitude=1.9),
}


def write(path,value):path.write_text(json.dumps(clean_json(value),ensure_ascii=False,indent=2)+'\n')


def setup(source,choice):
    amp=choice.get('amplitude',1.5);time=choice['time']
    cfg={**source['parameters'],'ticks':HOURS*720}
    for key in ['trend_window','mean_window','inventory_window']:cfg[key]=round(cfg[key]*time)
    cfg['tau']*=time
    cfg['mean_arrivals']=choice['arrival']
    cfg['order_sigma_foreign']*=amp/math.sqrt(time)
    cfg['entry_quantity']*=amp
    cfg['inventory_position_cap']*=amp
    cfg['inventory_sensitivity']/=amp
    if choice['slow_inventory']:cfg['inventory_adjustment']/=time
    cfg['psi']*=amp/math.sqrt(time)
    for key in ['take_profit','stop_loss','trail_arm','trail_drawdown']:cfg[key]*=amp
    profiles=[{**p,'sigma':p['sigma']*amp} for p in source['profiles']]
    return cfg,profiles,source['trend_multiplier']*time


def check(x,cfg):
    assert all(np.isfinite(v).all() for v in x.values())
    assert x['account_gold'].min()>=0 and x['account_foreign'].min()>=0
    assert not x['injection_gold'].any() and not x['injection_foreign'].any()
    np.testing.assert_allclose(x['gold']+x['account_gold'].sum(axis=1),600000,atol=1e-8,rtol=0)
    np.testing.assert_allclose(x['pool_foreign']+x['account_foreign'].sum(axis=1),120000,atol=1e-8,rtol=0)
    np.testing.assert_allclose(x['account_wealth'],x['account_gold']+x['account_foreign']*x['prices'][:,None],atol=1e-8,rtol=0)
    np.testing.assert_array_equal(x['account_trades'].sum(axis=1),x['trades'])


def metrics(x,e,hours):
    m=summarize(x,e,hours)
    # The amplitude target is1.5x, so primary wave definition uses a1.5% reversal
    # against source1%. Also retain the fixed1% definition for direct inspection.
    m['matched_waves']=swing_metrics(x['prices'][360:],.015)
    start=6*720
    if hours>6:
        m['after6h']=dict(trades_per_minute=float(x['trades'][start+1:].sum()/((len(x['prices'])-start-1)/12)),
            waves=swing_metrics(x['prices'][start:],.015))
    attempts=x['order_attempts'][361:].sum()
    m['clipped_fraction']=float(x['clipped_orders'][361:].sum()/attempts) if attempts else 0
    m['cash_saturated_arrivals']=int(x['cash_saturated'][361:].sum())
    m['foreign_saturated_arrivals']=int(x['foreign_saturated'][361:].sum())
    return m


def compact(rows):
    def avg(fn):return float(np.mean([fn(r) for r in rows]))
    return dict(trades_per_minute=avg(lambda r:r['trades_per_minute_after30']),
        cycle_minutes=avg(lambda r:r['matched_waves']['cycles_minutes']['median']),
        up_minutes=avg(lambda r:r['matched_waves']['up_minutes']['median']),
        up_gain=avg(lambda r:r['matched_waves']['up_gain']['median']),
        amplitude=avg(lambda r:r['matched_waves']['amplitude']['median']),
        fixed1pct_cycle_minutes=avg(lambda r:r['swings']['0.01']['cycles_minutes']['median']),
        clipped_fraction=avg(lambda r:r['clipped_fraction']),
        per_seed=[dict(trades=r['trades_per_minute_after30'],cycle=r['matched_waves']['cycles_minutes']['median'],
                       up=r['matched_waves']['up_minutes']['median'],gain=r['matched_waves']['up_gain']['median']) for r in rows])


def run_case(source,name,choice,seed,hours=HOURS):
    cfg,profiles,multiplier=setup(source,choice);cfg.update(ticks=hours*720,seeds=[seed])
    folder=OUT/name/f'{hours}h-seed{seed}';folder.mkdir(parents=True,exist_ok=True)
    params=dict(config=cfg,profiles=profiles,activity_b=source['activity_b'],trend_multiplier=multiplier)
    if (folder/'metrics.json').exists():
        assert json.loads((folder/'parameters.json').read_text())==params
        return json.loads((folder/'metrics.json').read_text())
    print('Running',name,hours,'hours',seed,flush=True)
    x,s,e,f,_,_=simulate(cfg,profiles,3,source['activity_b'],seed,{},source['policy'],
        {'trend_multiplier':multiplier,'episode_start':13})
    check(x,cfg)
    m=metrics(x,e,hours)
    save_trace(folder,'trace',x,e,f);write(folder/'summary.json',s)
    write(folder/'parameters.json',params);write(folder/'metrics.json',m)
    print('Finished',name,seed,compact([m]),flush=True)
    return m


def main():
    source=json.loads(SOURCE.read_text())
    hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [SOURCE,DAY]}
    write(OUT/'manifest.json',dict(source_candidate=source['name'],choices=CHOICES,seeds=SEEDS,hours=HOURS,
        amplitude_target_multiplier=1.5,target=dict(full_cycle_minutes=[40,60],one_way_minutes=[20,30],up_gain=.027,trades_per_minute=5),
        definitions='Primary waves1.5% reversal to match1.5x amplitude; fixed1% waves also reported. Exclude first30min, later-than6h sensitivity included.',
        scope='Offline warmup candidate only; original activity-day frozen preset untouched; no player, fee, shock, auto reset or schedule switching.',
        source_sha256=hashes))
    results={}
    for name,choice in CHOICES.items():
        results[name]={str(seed):run_case(source,name,choice,seed) for seed in SEEDS}
        write(OUT/'metrics.json',results)
    averages={name:compact(list(rows.values())) for name,rows in results.items()}
    for name,d in averages.items():
        d['score']=float(abs(math.log(d['cycle_minutes']/50))+abs(math.log(d['up_minutes']/25))+
            abs(math.log(d['up_gain']/.027))+abs(math.log(d['trades_per_minute']/5)))
    write(OUT/'averages.json',averages)
    assert all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in hashes.items())
    write(OUT/'audit.json',dict(candidate_paths=len(CHOICES)*len(SEEDS),assets_conserved=True,
        wallets_nonnegative=True,finite_series=True,wealth_and_trade_counts_verified=True,
        day_frozen_and_source_candidate_unchanged=True))
    print(json.dumps(averages,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
