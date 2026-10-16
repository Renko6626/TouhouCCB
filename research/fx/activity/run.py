"""Run four activity/execution cells without changing the production market."""
import argparse
import hashlib
import json
import math
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal
from pathlib import Path

import numpy as np

from research.fx.common.amm import Account, Pool
from research.fx.common.clock import draw_events, market_streams
from research.fx.common.execution import execute
from research.fx.common.analysis import return_diagnostics, state_diagnostics
from .model import ActivityState, linear_theory, load_config

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT = Path(__file__).parent/'results/reference'


def simulate(cfg, environment, b, seed):
    if environment not in ('linear', 'amm'):
        raise ValueError('unknown executor')
    model = ActivityState(b, cfg['tau'], cfg['mean_arrivals'], cfg['psi'], cfg['initial_z'])
    arrival_rngs, mark_rngs, execution_rng = market_streams(seed, cfg['accounts'])
    fields = ['returns', 'z_pre', 'intensity', 'reference_W', 'arrivals', 'raw_mark_sum',
              'raw_demand', 'raw_abs_demand']
    if environment == 'amm':
        fields += ['prices', 'gold', 'pool_foreign', 'pool_depth_W', 'trades', 'gold_volume',
                   'actual_flow', 'executed_abs_quantity', 'clipped_orders', 'skipped_orders',
                   'cash_saturated', 'foreign_saturated', 'wallet_gold_min', 'wallet_foreign_min']
    x = {k: np.zeros(cfg['ticks']+1) for k in fields}
    pool, accounts = None, []
    if environment == 'amm':
        pool = Pool(Decimal(str(cfg['pool_gold'])), Decimal(str(cfg['pool_foreign'])),
                    Decimal(str(cfg['fee_rate'])))
        accounts = [Account(str(i), Decimal(str(cfg['initial_gold'])), Decimal(str(cfg['initial_foreign'])))
                    for i in range(cfg['accounts'])]
        for field, value in [('prices', pool.price), ('gold', pool.gold), ('pool_foreign', pool.foreign),
                             ('wallet_gold_min', accounts[0].gold), ('wallet_foreign_min', accounts[0].foreign)]:
            x[field][0] = float(value)
        initial_gold = pool.gold+sum(a.gold for a in accounts)
        initial_foreign = pool.foreign+sum(a.foreign for a in accounts)
    sigma = cfg['order_sigma_foreign']
    for tick in range(1, cfg['ticks']+1):
        lam = model.intensity
        x['z_pre'][tick], x['intensity'][tick] = model.z, lam
        x['reference_W'][tick] = cfg['psi']**2*lam
        if pool is not None:
            previous_price = float(pool.price)
            x['pool_depth_W'][tick] = (2*sigma/float(pool.foreign))**2*lam
        events = draw_events(arrival_rngs, lam/cfg['accounts'], execution_rng)
        x['arrivals'][tick] = len(events)
        for i in events:
            epsilon = float(mark_rngs[i].normal())
            wanted = sigma*epsilon
            x['raw_mark_sum'][tick] += epsilon
            x['raw_demand'][tick] += wanted
            x['raw_abs_demand'][tick] += abs(wanted)
            if pool is None:
                continue
            account = accounts[i]
            # Zero-balance counters use the event's pre-trade wallet. A clipped
            # partial order is counted separately from an entirely skipped one.
            x['cash_saturated'][tick] += int(wanted > 0 and account.gold <= 0)
            x['foreign_saturated'][tick] += int(wanted < 0 and account.foreign <= 0)
            before = account.foreign
            quote = execute(pool, account, wanted, cfg['max_pool_fraction'])
            if quote is None:
                x['skipped_orders'][tick] += 1
                x['clipped_orders'][tick] += 1
                continue
            actual = float(account.foreign-before)
            x['clipped_orders'][tick] += int(abs(actual)+2e-6 < abs(wanted))
            x['trades'][tick] += 1
            x['actual_flow'][tick] += actual
            x['executed_abs_quantity'][tick] += abs(actual)
            x['gold_volume'][tick] += float(quote.input_amount if wanted > 0 else quote.output_amount)
        if pool is None:
            x['returns'][tick] = cfg['psi']*x['raw_mark_sum'][tick]
        else:
            x['prices'][tick] = float(pool.price)
            x['gold'][tick], x['pool_foreign'][tick] = float(pool.gold), float(pool.foreign)
            x['wallet_gold_min'][tick] = float(min(a.gold for a in accounts))
            x['wallet_foreign_min'][tick] = float(min(a.foreign for a in accounts))
            x['returns'][tick] = math.log(x['prices'][tick]/previous_price)
        model.advance(x['returns'][tick])
    summary = dict(environment=environment, b=b, seed=seed, ticks=cfg['ticks'], final_z=model.z,
                   arrivals=int(x['arrivals'].sum()), peak_intensity=float(x['intensity'].max()))
    if pool is not None:
        conserved = (pool.gold+sum(a.gold for a in accounts) == initial_gold and
                     pool.foreign+sum(a.foreign for a in accounts) == initial_foreign)
        nonnegative = all(a.gold >= 0 and a.foreign >= 0 for a in accounts)
        if not conserved or not nonnegative:
            raise AssertionError('AMM asset conservation or nonnegative-wallet failure')
        summary.update(pool_gold=str(pool.gold), pool_foreign=str(pool.foreign),
                       accounts=[dict(name=a.name, gold=str(a.gold), foreign=str(a.foreign)) for a in accounts],
                       conserved=conserved, nonnegative_wallets=nonnegative,
                       trades=int(x['trades'].sum()), gold_volume=float(x['gold_volume'].sum()))
    return x, summary


def source_hashes():
    paths = [Path(__file__), Path(__file__).with_name('model.py')]
    paths += sorted((Path(__file__).parents[1]/'common').glob('*.py'))
    paths += [ROOT/'backend/app/services/fx'/name for name in ['amm.py', 'quantize.py']]
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def clean_json(value):
    """Undefined diagnostics are JSON null, including constant-rate ACF."""
    if isinstance(value, dict):
        return {k: clean_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_json(v) for v in value]
    if isinstance(value, (float, np.floating)) and not math.isfinite(value):
        return None
    return value


def run_one(task):
    cfg, environment, b, seed, output = task
    name = f'{environment}-b{b:g}-seed{seed}'
    directory = Path(output)/name
    directory.mkdir(parents=True, exist_ok=True)
    try:
        x, summary = simulate(cfg, environment, b, seed)
    except (ValueError, OverflowError, FloatingPointError) as exc:
        (directory/'invalid.json').write_text(json.dumps(dict(environment=environment, b=b, seed=seed,
            config=cfg, status='invalid', reason=str(exc), source_sha256=source_hashes()), indent=2)+'\n')
        raise
    sample = slice(cfg['burn_in']+1, None)
    r, lam, z = (x[k][sample] for k in ['returns', 'intensity', 'z_pre'])
    diagnostics = return_diagnostics(r)
    diagnostics['activity'] = state_diagnostics(lam, r)
    diagnostics['moments'] = dict(mean_intensity=float(lam.mean()), variance_intensity=float(lam.var()),
        mean_z2=float(np.mean(z*z)), mean_z4=float(np.mean(z**4)),
        mean_reference_W=float(np.mean(x['reference_W'][sample])), mean_r2=float(np.mean(r*r)),
        r2_over_reference_W=float(np.mean(r*r)/np.mean(x['reference_W'][sample])),
        mean_arrivals=float(np.mean(x['arrivals'][sample])), peak_intensity=float(lam.max()))
    if environment == 'amm':
        diagnostics['pool_depth'] = state_diagnostics(x['pool_depth_W'][sample], r)
        diagnostics['moments']['r2_over_depth_W'] = float(np.mean(r*r)/np.mean(x['pool_depth_W'][sample]))
        arrivals = x['arrivals'][sample].sum()
        diagnostics['execution'] = {k:float(x[k][sample].sum()) for k in ['arrivals', 'trades', 'clipped_orders',
            'skipped_orders', 'cash_saturated', 'foreign_saturated', 'gold_volume', 'raw_abs_demand', 'executed_abs_quantity']}
        diagnostics['execution']['clipped_fraction'] = float(x['clipped_orders'][sample].sum()/arrivals)
        diagnostics['execution']['executed_fraction_of_raw_abs'] = float(x['executed_abs_quantity'][sample].sum()/x['raw_abs_demand'][sample].sum())
    np.savez_compressed(directory/'series.npz', **x)
    result = clean_json(dict(config=cfg, summary=summary, diagnostics=diagnostics,
        linear_theory=linear_theory(b, cfg['tau'], cfg['mean_arrivals']), source_sha256=source_hashes()))
    (directory/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    return dict(name=name, **summary, moments=diagnostics['moments'])


def validate_fixed_controls(output, seeds):
    archive = Path(__file__).parents[1]/'archive/mr-2026-10-15/results/switching-v2'
    checked = {}
    for seed in seeds:
        with np.load(Path(output)/f'amm-b0-seed{seed}/series.npz') as new, np.load(archive/f'noise-seed{seed}/series.npz') as old:
            fields = dict(prices='prices', gold='gold', arrivals='arrivals', raw_demand='noise_demand',
                          trades='trades', gold_volume='gold_volume')
            checked[str(seed)] = {key: bool(np.array_equal(new[key], old[value])) for key, value in fields.items()}
        if not all(checked[str(seed)].values()):
            raise AssertionError(f'archived fixed-rate control differs: seed {seed}: {checked[str(seed)]}')
    return checked


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--workers', type=int, default=3)
    args = parser.parse_args()
    cfg = load_config(args.config)
    args.output.mkdir(parents=True, exist_ok=True)
    summaries = []
    # Finish the linear reference before transferring the same rule to AMM.
    for environment in ['linear', 'amm']:
        tasks = [(cfg, environment, b, seed, str(args.output)) for b in cfg['b_values'] for seed in cfg['seeds']]
        with ProcessPoolExecutor(max_workers=args.workers) as executor:
            for summary in executor.map(run_one, tasks):
                summaries.append(summary)
                print(json.dumps(summary, ensure_ascii=False), flush=True)
    validation = validate_fixed_controls(args.output, cfg['seeds'])
    (args.output/'validation.json').write_text(json.dumps(dict(archived_full_path_exact_match=validation), indent=2)+'\n')
    (args.output/'summary.json').write_text(json.dumps(clean_json(summaries), ensure_ascii=False, indent=2, allow_nan=False)+'\n')


if __name__ == '__main__':
    main()
