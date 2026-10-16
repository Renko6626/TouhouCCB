"""Mix unchanged random-order and factor-target policies in one closed AMM."""
from concurrent.futures import ProcessPoolExecutor, as_completed
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import time
import traceback

import numpy as np

from backend.app.services.fx.quantize import amount_down
from research.fx.activity.model import ActivityState
from research.fx.activity.run import simulate as random_simulate
from research.fx.common.amm import Account, Pool
from research.fx.common.clock import market_streams, draw_events
from research.fx.common.execution import execute
from research.fx.factor_activity.model import components, target_position, execute_target
from research.fx.factor_activity.run import load_config, clean_json, hashes, ROOT
from research.fx.factor_activity.grid import compact_metrics
from research.fx.heterogeneous.profiles import make_profiles
from research.fx.heterogeneous.run import simulate as factor_simulate

HERE = Path(__file__).parent
OUTPUT = HERE / 'results/exploration'


def simulate(cfg, profiles, random_count, b, seed):
    n = cfg['accounts']
    if not 0 <= random_count <= n or len(profiles) != n:
        raise ValueError('invalid population')
    state = ActivityState(b, cfg['tau'], cfg['mean_arrivals'], cfg['psi'], cfg['initial_z'])
    arrival_rngs, mark_rngs, execution_rng = market_streams(seed, n)
    pool = Pool(Decimal(str(cfg['pool_gold'])), Decimal(str(cfg['pool_foreign'])), Decimal(str(cfg['fee_rate'])))
    initial_foreign = Decimal(str(cfg['initial_foreign']))
    accounts = [Account(str(i), Decimal(str(cfg['initial_gold'])), initial_foreign) for i in range(n)]
    fields = ['prices', 'gold', 'pool_foreign', 'returns', 'z_pre', 'intensity', 'arrivals', 'trades',
              'raw_demand', 'raw_abs_demand', 'actual_flow', 'executed_abs_quantity', 'gold_volume',
              'clipped_orders', 'skipped_orders', 'cash_saturated', 'foreign_saturated',
              'wallet_gold_min', 'wallet_foreign_min']
    group_fields = ['arrivals', 'trades', 'raw_abs_demand', 'executed_abs_quantity', 'actual_flow', 'clipped_orders']
    fields += [f'{group}_{field}' for group in ('random', 'factor') for field in group_fields]
    x = {k: np.zeros(cfg['ticks'] + 1) for k in fields}
    for key, value in [('prices', pool.price), ('gold', pool.gold), ('pool_foreign', pool.foreign),
                       ('wallet_gold_min', cfg['initial_gold']), ('wallet_foreign_min', initial_foreign)]:
        x[key][0] = float(value)
    logs = np.zeros(cfg['ticks'] + 1)
    logs[0] = math.log(float(pool.price))
    gold_total = pool.gold + sum(a.gold for a in accounts)
    foreign_total = pool.foreign + sum(a.foreign for a in accounts)
    for tick in range(1, cfg['ticks'] + 1):
        observed_price = pool.price
        lam = state.intensity
        x['intensity'][tick], x['z_pre'][tick] = lam, state.z
        window = max(cfg['trend_window'], cfg['mean_window'])
        past = logs[max(0, tick-window):tick]
        trend, mean, _ = components(past, logs[tick-1], 0, 1, 1, cfg)
        forecast = np.zeros(n)
        for i in range(random_count, n):
            p = profiles[i]
            terms = np.array([p['trend']*trend, p['reversion']*mean, p['sigma']*float(mark_rngs[i].normal())])
            forecast[i] = terms.sum()
        events = draw_events(arrival_rngs, lam/n, execution_rng)
        x['arrivals'][tick] = len(events)
        for i in events:
            random_policy = i < random_count
            group = 'random' if random_policy else 'factor'
            x[f'{group}_arrivals'][tick] += 1
            account = accounts[i]
            before, before_gold = account.foreign, account.gold
            if random_policy:
                wanted = cfg['order_sigma_foreign']*float(mark_rngs[i].normal())
                quote = execute(pool, account, wanted, cfg['max_pool_fraction'])
                raw = wanted
                clipped = quote is None or abs(float(account.foreign-before))+2e-6 < abs(wanted)
            else:
                target = target_position(float(forecast[i]), cfg)
                wanted = Decimal(str(cfg['adjustment']))*(target-(before-initial_foreign))
                quantity = amount_down(abs(wanted))
                unclipped_input = amount_down(quantity*observed_price) if wanted > 0 else quantity
                raw, quote = execute_target(pool, account, target, initial_foreign, observed_price, cfg)
                clipped = abs(raw) > 2e-6 if quote is None else quote.input_amount < unclipped_input
            x['raw_demand'][tick] += raw
            x['raw_abs_demand'][tick] += abs(raw)
            x[f'{group}_raw_abs_demand'][tick] += abs(raw)
            x['cash_saturated'][tick] += int(raw > 0 and before_gold <= 0)
            x['foreign_saturated'][tick] += int(raw < 0 and before <= 0)
            x['clipped_orders'][tick] += int(clipped)
            x[f'{group}_clipped_orders'][tick] += int(clipped)
            if quote is None:
                x['skipped_orders'][tick] += 1
                continue
            actual = float(account.foreign-before)
            for field, value in [('trades', 1), ('actual_flow', actual), ('executed_abs_quantity', abs(actual))]:
                x[field][tick] += value
                x[f'{group}_{field}'][tick] += value
            x['gold_volume'][tick] += float(quote.input_amount if raw > 0 else quote.output_amount)
        x['prices'][tick] = float(pool.price)
        x['gold'][tick], x['pool_foreign'][tick] = float(pool.gold), float(pool.foreign)
        x['wallet_gold_min'][tick] = float(min(a.gold for a in accounts))
        x['wallet_foreign_min'][tick] = float(min(a.foreign for a in accounts))
        logs[tick] = math.log(x['prices'][tick])
        x['returns'][tick] = math.log(x['prices'][tick]/x['prices'][tick-1])
        state.advance(x['returns'][tick])
    assert pool.gold + sum(a.gold for a in accounts) == gold_total
    assert pool.foreign + sum(a.foreign for a in accounts) == foreign_total
    assert all(a.gold >= 0 and a.foreign >= 0 for a in accounts)
    for field in group_fields:
        assert np.allclose(x[field], x[f'random_{field}']+x[f'factor_{field}'], atol=1e-9, rtol=1e-12)
    summary = dict(seed=seed, b=b, random_count=random_count, ticks=cfg['ticks'], final_z=state.z,
                   peak_intensity=float(x['intensity'].max()), conserved=True, nonnegative_wallets=True,
                   pool_gold=str(pool.gold), pool_foreign=str(pool.foreign),
                   accounts=[dict(name=a.name, policy='random' if i < random_count else 'factor',
                                  gold=str(a.gold), foreign=str(a.foreign)) for i, a in enumerate(accounts)])
    return x, summary


def metrics(x, summary, cfg):
    value = compact_metrics(x, summary, cfg)
    sample = slice(cfg['burn_in']+1, None)
    for group in ('random', 'factor'):
        for field in ['arrivals', 'trades', 'raw_abs_demand', 'executed_abs_quantity', 'clipped_orders']:
            value[f'{group}_{field}'] = float(x[f'{group}_{field}'][sample].sum())
        value[f'{group}_volume_share'] = value[f'{group}_executed_abs_quantity']/value['executed_abs_quantity']
    return clean_json(value)


def run_one(task):
    cfg, profiles, count, b, seed, sources = task
    name = f'random{count:02d}-b{b:g}-seed{seed}'
    directory = OUTPUT/name
    directory.mkdir(parents=True, exist_ok=True)
    result = dict(name=name, random_count=count, b=b, seed=seed, config=cfg, source_sha256=sources)
    start = time.perf_counter()
    try:
        x, summary = simulate(cfg, profiles, count, b, seed)
        result.update(status='complete', summary=summary, metrics=metrics(x, summary, cfg))
        thirds = np.array_split(np.arange(cfg['burn_in']+1, cfg['ticks']+1), 3)
        segments = []
        for index, positions in enumerate(thirds):
            sub = {k: v[positions] for k, v in x.items()}
            segments.append(dict(segment=index+1, first_tick=int(positions[0]), last_tick=int(positions[-1]),
                                 metrics=metrics(sub, {**summary, 'peak_intensity': float(sub['intensity'].max())},
                                                 {**cfg, 'burn_in': -1})))
        result['segments'] = segments
        np.savez_compressed(directory/'series.npz', **x)
    except Exception as exc:
        result.update(status='failed', error=f'{type(exc).__name__}: {exc}', traceback=traceback.format_exc())
    result['seconds'] = time.perf_counter()-start
    (directory/'result.json').write_text(json.dumps(clean_json(result), ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    return result


def main():
    spec = json.loads((HERE/'config.json').read_text())
    cfg = {**load_config(), 'ticks': spec['ticks'], 'burn_in': spec['burn_in'], 'seeds': spec['market_seeds']}
    profiles = make_profiles(cfg, spec['population_seed'], spec['spread'])
    sources = hashes()
    for path in [HERE/'run.py', HERE/'config.json', HERE.parent/'heterogeneous/profiles.py',
                 HERE.parent/'heterogeneous/run.py', HERE.parent/'factor_activity/grid.py']:
        sources[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    frozen = json.loads((HERE.parent/'activity/baseline-v1.json').read_text())['sha256']
    assert all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest() == h for p, h in frozen.items())
    manifest = dict(spec=spec, config=cfg, profiles=profiles, source_sha256=sources,
                    frozen_files_verified=len(frozen))
    path = OUTPUT/'manifest.json'
    if path.exists():
        assert json.loads(path.read_text()) == manifest
    else:
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')
    short = {**cfg, 'ticks': 300, 'burn_in': 30}
    compatibility = []
    for b in spec['b_values']:
        for count, oracle in [(0, factor_simulate), (cfg['accounts'], random_simulate)]:
            old, _ = oracle(short, profiles, b, 81) if count == 0 else oracle(short, 'amm', b, 81)
            new, _ = simulate(short, profiles, count, b, 81)
            common = sorted(old.keys() & new.keys())
            assert all(np.array_equal(old[k], new[k]) for k in common), (count, b)
            compatibility.append(dict(random_count=count, b=b, exact_common_fields=common))
    (OUTPUT/'compatibility.json').write_text(json.dumps(compatibility, indent=2)+'\n')
    tasks = []
    records = []
    for count in spec['random_counts']:
        for b in spec['b_values']:
            for seed in spec['market_seeds']:
                path = OUTPUT/f'random{count:02d}-b{b:g}-seed{seed}'/'result.json'
                if path.exists():
                    record = json.loads(path.read_text())
                    assert record['source_sha256'] == sources and record['config'] == cfg
                    records.append(record)
                else:
                    tasks.append((cfg, profiles, count, b, seed, sources))
    print(json.dumps(dict(cached=len(records), pending=len(tasks), compatibility='passed')), flush=True)
    with ProcessPoolExecutor(max_workers=spec['workers']) as executor:
        for future in as_completed([executor.submit(run_one, task) for task in tasks]):
            record = future.result()
            records.append(record)
            print(json.dumps(dict(done=len(records), name=record['name'], status=record['status'],
                                  seconds=round(record['seconds'], 1))), flush=True)
    records.sort(key=lambda r: r['name'])
    (OUTPUT/'summary.json').write_text(json.dumps(clean_json(records), ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    assert len(records) == 30 and all(r['status'] == 'complete' for r in records)


if __name__ == '__main__':
    main()
