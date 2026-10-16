"""Compare existing mixed policies with/without one explicitly recorded issue."""
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
from research.fx.common.amm import Account, Pool
from research.fx.common.clock import market_streams, draw_events
from research.fx.common.execution import execute
from research.fx.factor_activity.model import components, target_position, execute_target
from research.fx.factor_activity.run import load_config, clean_json, hashes, ROOT
from research.fx.heterogeneous.profiles import make_profiles
from research.fx.mixed.run import simulate as original_simulate

HERE = Path(__file__).parent
OUTPUT = HERE/'results/gold100k'


def simulate(cfg, profiles, random_count, b, seed, shock_tick, injection_gold):
    n = cfg['accounts']
    if not 0 <= random_count <= n or len(profiles) != n:
        raise ValueError('invalid population')
    if not 1 <= shock_tick <= cfg['ticks'] or injection_gold < 0:
        raise ValueError('invalid injection')
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
    fields += ['injection_gold', 'random_net_position', 'factor_net_position',
               'factor_target_mean', 'factor_trend_mean', 'factor_reversion_mean']
    x = {k: np.zeros(cfg['ticks']+1) for k in fields}
    for key, value in [('prices', pool.price), ('gold', pool.gold), ('pool_foreign', pool.foreign),
                       ('wallet_gold_min', cfg['initial_gold']), ('wallet_foreign_min', initial_foreign)]:
        x[key][0] = float(value)
    logs = np.zeros(cfg['ticks']+1)
    logs[0] = math.log(float(pool.price))
    gold_total = pool.gold+sum(a.gold for a in accounts)
    foreign_total = pool.foreign+sum(a.foreign for a in accounts)
    issue = None
    for tick in range(1, cfg['ticks']+1):
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
        if random_count < n:
            x['factor_target_mean'][tick] = np.mean([float(target_position(float(forecast[i]), cfg)) for i in range(random_count,n)])
            x['factor_trend_mean'][tick] = np.mean([p['trend']*trend for p in profiles[random_count:]])
            x['factor_reversion_mean'][tick] = np.mean([p['reversion']*mean for p in profiles[random_count:]])
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
                clipped = abs(raw)>2e-6 if quote is None else quote.input_amount < unclipped_input
            x['raw_demand'][tick] += raw
            x['raw_abs_demand'][tick] += abs(raw)
            x[f'{group}_raw_abs_demand'][tick] += abs(raw)
            x['cash_saturated'][tick] += int(raw>0 and before_gold<=0)
            x['foreign_saturated'][tick] += int(raw<0 and before<=0)
            x['clipped_orders'][tick] += int(clipped)
            x[f'{group}_clipped_orders'][tick] += int(clipped)
            if quote is None:
                x['skipped_orders'][tick] += 1
                continue
            actual = float(account.foreign-before)
            for field, value in [('trades',1), ('actual_flow',actual), ('executed_abs_quantity',abs(actual))]:
                x[field][tick] += value
                x[f'{group}_{field}'][tick] += value
            x['gold_volume'][tick] += float(quote.input_amount if raw>0 else quote.output_amount)
        if tick == shock_tick:
            issue = dict(tick=tick, amount=injection_gold, pre_price=str(pool.price),
                         pre_gold=str(pool.gold), pre_foreign=str(pool.foreign), pre_k=str(pool.gold*pool.foreign))
            if injection_gold:
                pool.inject(gold=Decimal(str(injection_gold)))
            issue.update(post_price=str(pool.price), post_gold=str(pool.gold), post_foreign=str(pool.foreign),
                         post_k=str(pool.gold*pool.foreign))
            x['injection_gold'][tick] = injection_gold
        x['prices'][tick] = float(pool.price)
        x['gold'][tick], x['pool_foreign'][tick] = float(pool.gold), float(pool.foreign)
        x['wallet_gold_min'][tick] = float(min(a.gold for a in accounts))
        x['wallet_foreign_min'][tick] = float(min(a.foreign for a in accounts))
        for group, subset in [('random',accounts[:random_count]), ('factor',accounts[random_count:])]:
            x[f'{group}_net_position'][tick] = float(sum((a.foreign-initial_foreign for a in subset), Decimal(0)))
        logs[tick] = math.log(x['prices'][tick])
        x['returns'][tick] = math.log(x['prices'][tick]/x['prices'][tick-1])
        state.advance(x['returns'][tick])
    assert pool.gold+sum(a.gold for a in accounts) == gold_total+Decimal(str(injection_gold))
    assert pool.foreign+sum(a.foreign for a in accounts) == foreign_total
    assert all(a.gold>=0 and a.foreign>=0 for a in accounts)
    summary = dict(seed=seed, b=b, random_count=random_count, ticks=cfg['ticks'], final_z=state.z,
                   issue=issue, peak_intensity=float(x['intensity'].max()), conserved_with_issue=True,
                   pool_gold=str(pool.gold), pool_foreign=str(pool.foreign),
                   accounts=[dict(name=a.name, policy='random' if i<random_count else 'factor',
                                  gold=str(a.gold), foreign=str(a.foreign)) for i,a in enumerate(accounts)])
    return x, summary


def pair_metrics(shock, control, summary, spec):
    t0 = spec['shock_tick']
    pre, post = (float(summary['issue'][k]) for k in ('pre_price','post_price'))
    jump = post-pre
    price = shock['prices']

    def response(ps, pc):
        return dict(price=float(ps), control_price=float(pc), price_change_from_pre=float(ps/pre-1),
                    retraced_fraction=float((post-ps)/jump), paired_retraced_fraction=float(1-(ps-pc)/jump))

    horizons = []
    for h in spec['horizons']:
        end = t0+h
        width = min(100,h)
        horizons.append(dict(horizon=h, point=response(price[end],control['prices'][end]),
                             trailing_width=width, trailing=response(price[end-width+1:end+1].mean(),
                                                                    control['prices'][end-width+1:end+1].mean())))
    start = len(price)-spec['terminal_window']
    terminal = response(price[start:].mean(),control['prices'][start:].mean())
    terminal['within_pre_one_percent'] = bool(abs(terminal['price_change_from_pre'])<=.01)
    post_prices = price[t0+1:]
    for name, limit in [('half',pre+.5*jump), ('full',pre)]:
        hits = np.flatnonzero(post_prices<=limit)
        terminal[f'first_{name}_hit'] = int(hits[0]+1) if len(hits) else None
    terminal['additional_runup_300'] = float(price[t0:t0+301].max()/post-1)
    for group in ('random','factor'):
        terminal[f'{group}_net_sold'] = float(-shock[f'{group}_actual_flow'][t0+1:].sum())
        terminal[f'{group}_control_net_sold'] = float(-control[f'{group}_actual_flow'][t0+1:].sum())
    terminal['peak_post_intensity'] = float(shock['intensity'][t0+1:].max())
    terminal['post_clipped_fraction'] = float(shock['clipped_orders'][t0+1:].sum()/shock['arrivals'][t0+1:].sum())
    terminal['wallet_gold_min'] = float(shock['wallet_gold_min'][t0:].min())
    terminal['wallet_foreign_min'] = float(shock['wallet_foreign_min'][t0:].min())
    return dict(pre_price=pre, post_price=post, injection_jump=jump/pre, horizons=horizons, terminal=terminal)


def run_one(task):
    cfg, profiles, count, b, seed, spec, sources = task
    name = f'random{count:02d}-b{b:g}-seed{seed}'
    directory = OUTPUT/name
    directory.mkdir(parents=True, exist_ok=True)
    record = dict(name=name, random_count=count, b=b, seed=seed, config=cfg, source_sha256=sources)
    start = time.perf_counter()
    try:
        control, cs = simulate(cfg,profiles,count,b,seed,spec['shock_tick'],0)
        shock, ss = simulate(cfg,profiles,count,b,seed,spec['shock_tick'],spec['injection_gold'])
        assert all(np.array_equal(shock[k][:spec['shock_tick']],control[k][:spec['shock_tick']]) for k in control)
        assert cs['issue']['pre_price'] == ss['issue']['pre_price']
        record.update(status='complete', control_summary=cs, shock_summary=ss,
                      response=pair_metrics(shock,control,ss,spec))
        np.savez_compressed(directory/'control.npz',**control)
        np.savez_compressed(directory/'shock.npz',**shock)
    except Exception as exc:
        record.update(status='failed',error=f'{type(exc).__name__}: {exc}',traceback=traceback.format_exc())
    record['seconds'] = time.perf_counter()-start
    (directory/'result.json').write_text(json.dumps(clean_json(record),ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    return record


def main():
    spec = json.loads((HERE/'config.json').read_text())
    cfg = {**load_config(), 'ticks': spec['ticks'], 'burn_in':spec['shock_tick'], 'seeds':spec['market_seeds']}
    profiles = make_profiles(cfg,spec['population_seed'],spec['spread'])
    sources = hashes()
    for path in [HERE/'run.py',HERE/'config.json',HERE.parent/'mixed/run.py',HERE.parent/'heterogeneous/profiles.py']:
        sources[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    OUTPUT.mkdir(parents=True,exist_ok=True)
    frozen = json.loads((HERE.parent/'activity/baseline-v1.json').read_text())['sha256']
    assert all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in frozen.items())
    manifest = dict(spec=spec,config=cfg,profiles=profiles,source_sha256=sources,frozen_files_verified=len(frozen))
    path = OUTPUT/'manifest.json'
    if path.exists():assert json.loads(path.read_text())==manifest
    else:path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    short = {**cfg,'ticks':200}
    compatibility = []
    for count in spec['random_counts']:
        for b in spec['b_values']:
            old,_ = original_simulate(short,profiles,count,b,81)
            new,_ = simulate(short,profiles,count,b,81,100,0)
            assert all(np.array_equal(new[k],old[k]) for k in old), (count,b)
            compatibility.append(dict(random_count=count,b=b,exact_fields=len(old)))
    (OUTPUT/'compatibility.json').write_text(json.dumps(compatibility,indent=2)+'\n')
    tasks, records = [], []
    for count in spec['random_counts']:
        for b in spec['b_values']:
            for seed in spec['market_seeds']:
                path = OUTPUT/f'random{count:02d}-b{b:g}-seed{seed}'/'result.json'
                if path.exists():
                    r = json.loads(path.read_text())
                    assert r['config']==cfg and r['source_sha256']==sources
                    records.append(r)
                else:tasks.append((cfg,profiles,count,b,seed,spec,sources))
    print(json.dumps(dict(cached=len(records),pending_pairs=len(tasks),compatibility='passed')),flush=True)
    with ProcessPoolExecutor(max_workers=spec['workers']) as executor:
        for future in as_completed([executor.submit(run_one,t) for t in tasks]):
            r = future.result();records.append(r)
            print(json.dumps(dict(done_pairs=len(records),name=r['name'],status=r['status'],seconds=round(r['seconds'],1))),flush=True)
    records.sort(key=lambda r:r['name'])
    (OUTPUT/'summary.json').write_text(json.dumps(clean_json(records),ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    assert len(records)==100 and all(r['status']=='complete' for r in records)


if __name__=='__main__':main()
