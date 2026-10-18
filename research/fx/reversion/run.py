"""Predeclared normal/up/down comparison with an unchanged old-policy control."""
from concurrent.futures import ProcessPoolExecutor,as_completed
from pathlib import Path
import hashlib
import json
import time

import numpy as np

from research.fx.episodes.run import source_hashes,save_trace
from research.fx.factor_activity.run import load_config,clean_json,ROOT
from research.fx.heterogeneous.profiles import make_profiles
from .model import simulate

HERE=Path(__file__).parent
OUTPUT=HERE/'results/exploration'


def one(task):
    scale,inventory_count,seed,cfg,profiles,spec=task
    name=f"{scale['id']}-inventory{inventory_count}-seed{seed}"
    directory=OUTPUT/name;directory.mkdir(parents=True,exist_ok=True)
    start=time.perf_counter();cases={};summary={}
    for label,jump in [('normal',0),('up',.05),('down',-.05)]:
        x,s,episodes,fills,inventory_fills=simulate(cfg,profiles,inventory_count,spec['b'],seed,spec['shock_tick'],jump)
        if label!='normal':
            assert all(np.array_equal(x[k][:spec['shock_tick']],cases['normal'][k][:spec['shock_tick']]) for k in x)
            observed=float(s['issue']['post_price'])/float(s['issue']['pre_price'])-1
            assert abs(observed-jump)<1e-11
        gold=x['gold']+x['random_gold']+x['factor_gold']+x['reversion_gold']
        np.testing.assert_allclose(gold,cfg['pool_gold']+cfg['accounts']*cfg['initial_gold']+np.cumsum(x['injection_gold']),atol=1e-8,rtol=0)
        np.testing.assert_allclose(x['pool_foreign']+x['random_net_position']+x['factor_net_position']+x['reversion_net_position'],
                                  cfg['pool_foreign']+np.cumsum(x['injection_foreign']),atol=1e-8,rtol=0)
        np.testing.assert_allclose(x['factor_gold']-5*cfg['initial_gold'],x['realized_pnl']-x['long_basis']+x['short_basis'],atol=1e-8,rtol=0)
        for ledger in [fills,inventory_fills]:assert len({(f['account'],f['tick']) for f in ledger})==len(ledger)
        assert x['wallet_gold_min'].min()>=0 and x['wallet_foreign_min'].min()>=0
        assert all(np.isfinite(v).all() for v in x.values())
        save_trace(directory,label,x,episodes,fills)
        (directory/f'{label}-inventory.jsonl').write_text(''.join(json.dumps(f,ensure_ascii=False)+'\n' for f in inventory_fills))
        cases[label]=x;summary[label]=s
    r=clean_json(dict(name=name,scale=scale,inventory_count=inventory_count,seed=seed,config=cfg,
                      summaries=summary,status='complete',seconds=time.perf_counter()-start))
    (directory/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    return r


def main():
    spec=json.loads((HERE/'config.json').read_text());cfg=load_config()
    cfg.update({k:spec[k] for k in ['ticks','entry_quantity','take_profit','stop_loss','trail_arm','trail_drawdown',
                                   'inventory_window','inventory_position_cap','inventory_sensitivity','inventory_adjustment']})
    cfg.update(burn_in=0,seeds=spec['market_seeds'])
    profiles=make_profiles(cfg,spec['population_seed'],spec['spread'])
    sources=source_hashes()
    for p in [HERE/'run.py',HERE/'model.py',HERE/'inventory.py',HERE/'config.json']:
        sources[str(p.relative_to(ROOT))]=hashlib.sha256(p.read_bytes()).hexdigest()
    protected=json.loads((HERE.parent/'episodes/rapid/results/manifest.json').read_text())['protected_sha256']
    for p,h in {**sources,**protected}.items():assert hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h,p
    OUTPUT.mkdir(parents=True,exist_ok=True)
    manifest=dict(spec=spec,config=cfg,profiles=profiles,source_sha256=sources,protected_sha256=protected)
    (OUTPUT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    tasks=[(scale,count,seed,{**cfg,**{k:v for k,v in scale.items() if k!='id'}},profiles,spec)
           for scale in spec['scales'] for count in spec['inventory_counts'] for seed in spec['market_seeds']]
    records=[]
    with ProcessPoolExecutor(max_workers=spec['workers']) as pool:
        futures=[pool.submit(one,t) for t in tasks]
        for f in as_completed(futures):
            r=f.result();records.append(r);print(f"{len(records)}/{len(tasks)} {r['name']} {r['seconds']:.1f}s",flush=True)
    assert len(records)==18
    for p,h in {**sources,**protected}.items():assert hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h,p
    (OUTPUT/'summary.json').write_text(json.dumps(sorted(records,key=lambda r:r['name']),ensure_ascii=False,indent=2)+'\n')
    print('Complete: 54 paths, 466560 ticks; frozen files unchanged.',flush=True)


if __name__=='__main__':main()
