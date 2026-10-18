"""Bounded parameter experiment: ~20min cycles, ~10min up legs, same fills/min.

python output/fx-cycle20-20261016/run.py
"""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import hashlib
import json
import numpy as np
from research.fx.intraday_6_8h.model import simulate
from research.fx.intraday_6_8h.run import summarize
from research.fx.episodes.run import save_trace
from research.fx.factor_activity.run import clean_json

OUT=Path(__file__).resolve().parent
SNAPSHOT=ROOT/'research/fx/intraday_6_8h/FROZEN_CONFIG.json'
SEEDS=[20261029,20261030,20261031]
HOURS=7
CANDIDATES={
    'memory2':dict(memory=2,exits=1,random_size=1),
    'memory3':dict(memory=3,exits=1,random_size=1),
    'memory2_exit15':dict(memory=2,exits=1.5,random_size=1),
    'memory2_exit2':dict(memory=2,exits=2,random_size=1),
    'memory2_noise75':dict(memory=2,exits=1,random_size=.75),
    'memory2_noise67':dict(memory=2,exits=1,random_size=2/3),
    # Follow-up after the first six cases: combine the effective controls and
    # compensate arrivals where slower exits reduce the observed fill rate.
    'memory2_noise67_exit15':dict(memory=2,exits=1.5,random_size=2/3),
    'memory2_noise67_exit2':dict(memory=2,exits=2,random_size=2/3),
    'memory2_noise50_exit15':dict(memory=2,exits=1.5,random_size=.5),
    'memory2_noise50_exit15_rate07':dict(memory=2,exits=1.5,random_size=.5,arrival=.7),
    'memory2_noise72_exit2_rate07':dict(memory=2,exits=2,random_size=72/108,arrival=.7),
    'memory2_noise66_exit2_rate07':dict(memory=2,exits=2,random_size=66/108,arrival=.7),
    'memory2_noise60_exit2_rate07':dict(memory=2,exits=2,random_size=60/108,arrival=.7),
}


def write(path,value):path.write_text(json.dumps(clean_json(value),ensure_ascii=False,indent=2)+'\n')


def setup(frozen,choice):
    cfg={**frozen['parameters'],'ticks':HOURS*720}
    memory=choice['memory']
    for key in ['trend_window','mean_window','inventory_window']:cfg[key]=round(cfg[key]*memory)
    cfg['tau']*=memory
    for key in ['take_profit','stop_loss','trail_arm','trail_drawdown']:cfg[key]*=choice['exits']
    cfg['order_sigma_foreign']*=choice['random_size']
    cfg['mean_arrivals']=choice.get('arrival',.6)
    # Average-per-tick trend shrinks when its denominator grows. Compensate that
    # arithmetic scaling; this does not preserve pathwise signals or future returns.
    multiplier=frozen['trend_multiplier']*memory
    return cfg,multiplier


def check(x,cfg):
    assert all(np.isfinite(v).all() for v in x.values())
    assert x['account_gold'].min()>=0 and x['account_foreign'].min()>=0
    assert not x['injection_gold'].any() and not x['injection_foreign'].any()
    np.testing.assert_allclose(x['gold']+x['account_gold'].sum(axis=1),600000,atol=1e-8,rtol=0)
    np.testing.assert_allclose(x['pool_foreign']+x['account_foreign'].sum(axis=1),120000,atol=1e-8,rtol=0)
    np.testing.assert_allclose(x['account_wealth'],x['account_gold']+x['account_foreign']*x['prices'][:,None],atol=1e-8,rtol=0)
    np.testing.assert_array_equal(x['account_trades'].sum(axis=1),x['trades'])


def compact(rows):
    return dict(trades_per_minute=float(np.mean([r['trades_per_minute_after30'] for r in rows])),
        cycle1_minutes=float(np.mean([r['swings']['0.01']['cycles_minutes']['median'] for r in rows])),
        up1_minutes=float(np.mean([r['swings']['0.01']['up_minutes']['median'] for r in rows])),
        amplitude1=float(np.mean([r['swings']['0.01']['amplitude']['median'] for r in rows])),
        range30=float(np.mean([r['range30']['median'] for r in rows])),
        holding_median=float(np.mean([r['completed_holding_minutes']['median'] for r in rows])),
        per_seed=[dict(trades=r['trades_per_minute_after30'],cycle=r['swings']['0.01']['cycles_minutes']['median'],
                       up=r['swings']['0.01']['up_minutes']['median']) for r in rows])


def main():
    frozen=json.loads(SNAPSHOT.read_text())
    protected=[SNAPSHOT,ROOT/'research/fx/intraday_6_8h/model.py',ROOT/'research/fx/intraday_6_8h/BASELINE_METRICS.json']
    hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    write(OUT/'manifest.json',dict(candidates=CANDIDATES,seeds=SEEDS,hours=HOURS,
        target=dict(cycle_minutes=20,up_minutes=10,trades_per_minute=5.2444444444444445),
        scope='Offline parameter changes only; no players/shocks/fees, frozen version retained. First six cases fixed arrivals0.6; follow-up combos permit explicit arrival compensation.',
        source_sha256=hashes))
    baseline=json.loads((ROOT/'research/fx/intraday_6_8h/BASELINE_METRICS.json').read_text())
    results={'baseline':{s:r[str(HOURS)] for s,r in baseline['by_seed'].items()}}
    for name,choice in CANDIDATES.items():
        cfg,multiplier=setup(frozen,choice);results[name]={}
        for seed in SEEDS:
            folder=OUT/name/f'seed{seed}';folder.mkdir(parents=True,exist_ok=True)
            params=dict(config={**cfg,'seeds':[seed]},trend_multiplier=multiplier,
                        activity_b=frozen['activity_b'],profiles=frozen['profiles'])
            if (folder/'metrics.json').exists():
                assert json.loads((folder/'parameters.json').read_text())==params
                results[name][str(seed)]=json.loads((folder/'metrics.json').read_text());continue
            print('Running',name,seed,flush=True)
            x,s,e,f,_,_=simulate(params['config'],frozen['profiles'],3,frozen['activity_b'],seed,{},
                frozen['policy'],{'trend_multiplier':multiplier,'episode_start':13})
            check(x,cfg)
            m=summarize(x,e,HOURS)
            save_trace(folder,'trace',x,e,f);write(folder/'summary.json',s)
            write(folder/'parameters.json',params);write(folder/'metrics.json',m)
            results[name][str(seed)]=m
            print('Finished',name,seed,compact([m]),flush=True)
        write(OUT/'metrics.json',results)
    averages={name:compact(list(rows.values())) for name,rows in results.items()}
    for name,d in averages.items():
        d['score']=float(abs(np.log(d['cycle1_minutes']/20))+abs(np.log(d['up1_minutes']/10))+
            2*abs(np.log(d['trades_per_minute']/averages['baseline']['trades_per_minute'])))
    write(OUT/'averages.json',averages)
    assert all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in hashes.items())
    write(OUT/'audit.json',dict(candidate_paths=len(CANDIDATES)*len(SEEDS),finite_series=True,nonnegative_wallets=True,
        assets_conserved=True,wealth_and_trade_counts_verified=True,frozen_sources_unchanged=True))
    print(json.dumps(averages,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
