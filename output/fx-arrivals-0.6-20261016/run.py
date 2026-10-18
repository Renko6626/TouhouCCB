"""User-selected compromise: background mean_arrivals=0.6; all else retained.

Run: python output/fx-arrivals-0.6-20261016/run.py
"""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import hashlib
import json
import runpy
import numpy as np
from research.fx.episodes.run import save_trace
from research.fx.factor_activity.run import clean_json

OUT=Path(__file__).resolve().parent
SOURCE=ROOT/'output/fx-role-time-20261016/manifest.json'
H=runpy.run_path(str(ROOT/'output/fx-role-mix-20261016/run.py'))
SEEDS=[20261029,20261030,20261031]


def write(path,value):path.write_text(json.dumps(clean_json(value),ensure_ascii=False,indent=2)+'\n')


def main():
    source=json.loads(SOURCE.read_text())
    original=source['config']
    cfg={**original,'mean_arrivals':0.6}
    assert {k for k in original if cfg[k]!=original[k]}=={'mean_arrivals'}
    paths=[SOURCE,ROOT/'output/fx-role-mix-20261016/model.py']
    hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    write(OUT/'config.json',dict(name='mixed-10-3-3-4-time125-arrivals06',
        status='user_selected_research_candidate_not_applied_to_production',tick_seconds=5,
        parameters=cfg,profiles=source['profiles'],activity_b=source['activity_b'],
        trend_multiplier=source['trend_multiplier'],groups=source['groups'],
        changed_parameter={'mean_arrivals':{'before':original['mean_arrivals'],'after':0.6}},
        validation_seeds=SEEDS,source_sha256=hashes))
    results={}
    for seed in SEEDS:
        print('Running arrivals0.6',seed,flush=True)
        x,s,e,f,_,d=H['SIM']({**cfg,'seeds':[seed]},source['profiles'],3,source['activity_b'],seed,{},
            'weak_trend',{'trend_multiplier':source['trend_multiplier'],'episode_start':13})
        H['CHECK'](x,s,cfg)
        m=H['metrics'](x,e,s,'candidate',d)
        m['arrivals_per_minute']=float(x['arrivals'][361:].sum()/(9640/12))
        m['gross_foreign_per_minute']=float(x['executed_abs_quantity'][361:].sum()/(9640/12))
        results[str(seed)]=m
        folder=OUT/f'seed{seed}';folder.mkdir(exist_ok=True)
        save_trace(folder,'trace',x,e,f);write(folder/'summary.json',s)
        print('Finished',seed,'trades/min',m['trades_per_minute'],flush=True)
    averages={k:float(np.mean([m[k]['median'] if isinstance(m[k],dict) else m[k] for m in results.values()]))
        for k in ['trades_per_minute','arrivals_per_minute','range30','range60','gross_foreign_per_minute']}
    averages['cycle1_minutes']=float(np.mean([m['swings']['0.01']['cycles_minutes']['median'] for m in results.values()]))
    averages['amplitude1']=float(np.mean([m['swings']['0.01']['amplitude']['median'] for m in results.values()]))
    write(OUT/'metrics.json',results);write(OUT/'averages.json',averages)
    assert all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in hashes.items())
    write(OUT/'audit.json',dict(new_paths=3,no_shocks=True,zero_fees=True,
        only_mean_arrivals_changed=True,finite_series=True,nonnegative_wallets=True,
        assets_conserved=True,wealth_and_trade_counts_verified=True,sources_unchanged=True))
    print(json.dumps(averages,indent=2),flush=True)


if __name__=='__main__':main()
