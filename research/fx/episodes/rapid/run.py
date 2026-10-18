"""Shorter exits versus more frequent arrivals; unchanged policy implementation."""
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
import hashlib
import json
import time

import numpy as np

from research.fx.episodes.run import simulate, save_trace, source_hashes
from research.fx.factor_activity.run import load_config, clean_json, ROOT
from research.fx.heterogeneous.profiles import make_profiles

HERE = Path(__file__).parent
OUTPUT = HERE/'results'


def one(task):
    cell, count, seed, cfg, profiles, spec = task
    name = f"{cell['id']}-random{count:02d}-seed{seed}"
    directory = OUTPUT/name
    directory.mkdir(parents=True, exist_ok=True)
    start = time.perf_counter()
    control, cs, ce, cf = simulate(cfg, profiles, count, spec['b'], seed, spec['variant'], spec['shock_tick'], 0)
    shock, ss, se, sf = simulate(cfg, profiles, count, spec['b'], seed, spec['variant'], spec['shock_tick'], spec['injection_gold'])
    t0 = spec['shock_tick']
    assert all(np.array_equal(shock[k][:t0], control[k][:t0]) for k in control)
    assert cs['issue']['pre_price'] == ss['issue']['pre_price']
    if cell['id'] == 'original':
        old = HERE.parent/'results/exploration/normal'/f'trailing-random{count:02d}-b0.25-seed{seed}'/'series.npz'
        with np.load(old) as x:
            assert all(np.array_equal(control[k], x[k][:cfg['ticks']+1]) for k in control)
    for label, x, episodes, fills in [('control', control, ce, cf), ('shock', shock, se, sf)]:
        assert all(np.isfinite(v).all() for v in x.values())
        issued = np.cumsum(x['injection_gold'])
        np.testing.assert_allclose(x['gold']+x['random_gold']+x['factor_gold'],
                                   cfg['pool_gold']+cfg['accounts']*cfg['initial_gold']+issued, atol=1e-8, rtol=0)
        np.testing.assert_allclose(x['pool_foreign']+x['random_net_position']+x['factor_net_position'],
                                   cfg['pool_foreign'], atol=1e-8, rtol=0)
        np.testing.assert_allclose(x['factor_gold']-(cfg['accounts']-count)*cfg['initial_gold'],
                                   x['realized_pnl']-x['long_basis']+x['short_basis'], atol=1e-8, rtol=0)
        assert len({(f['account'], f['tick']) for f in fills}) == len(fills)
        assert int(x['entries'].sum()) == len(episodes)
        assert int(x['completions'].sum()) == sum(e['close_tick'] is not None for e in episodes)
        assert x['wallet_gold_min'].min() >= 0 and x['wallet_foreign_min'].min() >= 0
        save_trace(directory, label, x, episodes, fills)
    result = dict(name=name, cell=cell, random_count=count, seed=seed, config=cfg,
                  control_summary=cs, shock_summary=ss, seconds=time.perf_counter()-start,
                  status='complete', prefix_exact=True, conservation_checked=True,
                  original_prefix_exact=cell['id']=='original')
    (directory/'result.json').write_text(json.dumps(clean_json(result), ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    return clean_json(result)


def main():
    spec = json.loads((HERE/'config.json').read_text())
    original_spec = json.loads((HERE.parent/'config.json').read_text())
    base = load_config()
    base.update({k:original_spec[k] for k in ['entry_quantity','take_profit','stop_loss','trail_arm','trail_drawdown']})
    base.update(ticks=max(spec['horizons']), burn_in=0, seeds=spec['market_seeds'])
    profiles = make_profiles(base, spec['population_seed'], spec['spread'])
    sources = source_hashes()
    for p in [HERE/'run.py', HERE/'config.json']:
        sources[str(p.relative_to(ROOT))] = hashlib.sha256(p.read_bytes()).hexdigest()
    protected = json.loads((HERE.parent/'results/exploration/preexisting_sha256.json').read_text())
    for p in [HERE.parent/'model.py',HERE.parent/'run.py',HERE.parent/'config.json']:
        protected[str(p.relative_to(ROOT))] = hashlib.sha256(p.read_bytes()).hexdigest()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT/'manifest.json').write_text(json.dumps(dict(spec=spec, base_config=base, profiles=profiles,
        source_sha256=sources, protected_sha256=protected), ensure_ascii=False, indent=2)+'\n')
    tasks = [(cell, count, seed, {**base, **{k:v for k,v in cell.items() if k not in ('id','label')}}, profiles, spec)
             for cell in spec['cells'] for count in spec['random_counts'] for seed in spec['market_seeds']]
    records = []
    with ProcessPoolExecutor(max_workers=spec['workers']) as pool:
        futures = [pool.submit(one, task) for task in tasks]
        for f in as_completed(futures):
            r = f.result()
            records.append(r)
            print(f"{len(records)}/{len(tasks)} {r['name']} {r['seconds']:.1f}s", flush=True)
    assert len(records)==36
    for p,h in {**sources, **protected}.items():
        assert hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h,p
    records.sort(key=lambda r:r['name'])
    (OUTPUT/'summary.json').write_text(json.dumps(records, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    print('Complete: 72 paths, 622080 ticks; existing files unchanged.', flush=True)


if __name__ == '__main__':
    main()
