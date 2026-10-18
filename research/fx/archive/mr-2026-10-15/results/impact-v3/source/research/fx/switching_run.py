"""Run fixed A, random switching, performance B, and a noise-only control."""
import argparse
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import random
from time import perf_counter

import numpy as np
from .amm import Pool
from .switching import Agent, execute, target_observations
from .diagnostics import acf, describe
from .hurst import dfa, fits

ROOT = Path(__file__).resolve().parent
SIZES = [16,24,32,48,64,96,128,192,256,384,512,768,1024,1536,2048,3072,4096,6144,8192]
MODES = ('fixed', 'random', 'performance', 'noise', 'fixed_m4', 'fixed_m12', 'matched_noise')


def read_config(path=None):
    cfg = json.loads(Path(path or ROOT/'configs/switching.json').read_text())
    assert sum(cfg['type_counts']) == cfg['bots']
    assert 0 <= cfg['exit_threshold'] < cfg['entry_threshold']
    assert 0 < cfg['adjustment'] <= 1 and 0 < cfg['max_pool_fraction'] < 1
    assert min(cfg['trend_window'], cfg['mean_window'], cfg['rate'], cfg['position_cap']) > 0
    assert 0 <= cfg['adaptive_per_type'] <= min(cfg['type_counts'][:2])
    assert 0 < cfg['choice_rate'] < 1 and all(0 < g <= 1 for g in cfg['score_gammas'])
    return cfg


def simulate(cfg, mode, seed, beta):
    if mode not in MODES:
        raise ValueError(f'unknown mode: {mode}')
    pool = Pool(Decimal(str(cfg['gold'])), Decimal(str(cfg['foreign'])), Decimal(str(cfg['fee_rate'])))
    p0 = float(pool.price)
    kinds = ['M'] * cfg['type_counts'][0] + ['R'] * cfg['type_counts'][1] + ['N'] * cfg['type_counts'][2]
    if mode == 'noise':
        kinds = ['N'] * cfg['bots']
    elif mode in ('fixed_m4', 'fixed_m12'):
        m = 4 if mode == 'fixed_m4' else 12
        kinds = ['M'] * m + ['R'] * (16-m) + ['N'] * 4
    elif mode == 'matched_noise':
        kinds = ['I'] * 16 + ['N'] * 4
    seen = {'M': 0, 'R': 0, 'N': 0, 'I': 0}
    agents = []
    for i, kind in enumerate(kinds):
        adaptive = kind in ('M', 'R') and seen[kind] < cfg['adaptive_per_type']
        seen[kind] += 1
        agents.append(Agent(i, kind, adaptive, seed, cfg, p0))
    n = cfg['ticks']
    # Precomputed exogenous demand, independent of executed trades and choices.
    schedules = [np.random.default_rng(seed*1000003+i*101+1).poisson(cfg['rate'], n) for i in range(len(agents))]
    noise_rngs = [np.random.default_rng(seed*1000003+i*101+2) for i in range(len(agents))]
    execution_rng = random.Random(seed*1000003+4)
    series = {k: np.zeros(n+1) for k in ('prices', 'gold', 'arrivals', 'noise_demand', 'trades',
               'gold_volume', 'trend_count', 'trend_equity_fraction', 'score_difference', 'cash_saturated', 'foreign_saturated',
               'demand_D', 'predicted_variance', 'predicted_mean', 'predicted_second_moment',
               'target_near_count', 'active_arrivals', 'inventory_strategy', 'inventory_noise', 'pool_foreign',
               'strategy_flow', 'noise_flow', 'trades_M', 'trades_R', 'trades_N', 'volume_M', 'volume_R', 'volume_N',
               'signal_strong_M', 'signal_strong_R', 'candidate_changes_M', 'candidate_changes_R')}
    series['prices'][0], series['gold'][0] = p0, cfg['gold']
    series['pool_foreign'][0] = cfg['foreign']
    series['trend_count'][0] = kinds.count('M')
    series['trend_equity_fraction'][0] = kinds.count('M') / len(agents)
    history = [math.log(p0)]
    initial_gold = pool.gold + sum(a.account.gold for a in agents)
    initial_foreign = pool.foreign + sum(a.account.foreign for a in agents)
    score_ss = score_n = 0
    budget_clips = 0
    previous_targets = [0] * len(agents)
    for tick in range(1, n+1):
        current = history[-1]
        held = [a.shadow_u.copy() for a in agents]
        for agent in agents:
            if agent.kind in ('M', 'R'):
                agent.observe(current, history)
                agent.reconsider(mode, beta)
        for key, value in target_observations(agents, previous_targets).items():
            series[key][tick] = value
        # Predict the upcoming interval before arrivals/trades, using the target
        # a trader would activate at its next wake (not its stale stored state).
        gaps = [a.intent_gap() for a in agents if a.kind in ('M', 'R')]
        demand = cfg['rate'] * cfg['adjustment']**2 * sum(g*g for g in gaps)
        impact = 2 / float(pool.foreign)
        noise_variance = kinds.count('N') * cfg['rate'] * (cfg['noise_gold_size']/p0)**2
        series['demand_D'][tick] = demand
        series['predicted_variance'][tick] = impact**2 * (demand + noise_variance)
        series['predicted_mean'][tick] = impact * cfg['rate'] * cfg['adjustment'] * sum(gaps)
        series['predicted_second_moment'][tick] = (series['predicted_variance'][tick]
                                                   + series['predicted_mean'][tick]**2)
        series['target_near_count'][tick] = sum(abs(g) <= .01*cfg['position_cap'] for g in gaps)
        events = [i for i, row in enumerate(schedules) for _ in range(row[tick-1])]
        execution_rng.shuffle(events)
        series['arrivals'][tick] = len(events)
        for i in events:
            agent = agents[i]
            if agent.kind == 'I':
                continue  # Keep its event in the shuffle to preserve pairing.
            series['active_arrivals'][tick] += 1
            if agent.kind == 'N':
                wanted = cfg['noise_gold_size'] / p0 * noise_rngs[i].normal()
                series['noise_demand'][tick] += wanted
            else:
                wanted = agent.wake()
            before_foreign = agent.account.foreign
            pool_capped = min(abs(wanted), float(pool.foreign) * cfg['max_pool_fraction'])
            quote = execute(pool, agent.account, wanted, cfg['max_pool_fraction'])
            if quote is not None:
                actual = abs(agent.account.foreign - before_foreign)
                budget_clips += float(actual) + .000002 < pool_capped
                series['trades'][tick] += 1
                volume = float(quote.input_amount if wanted > 0 else quote.output_amount)
                series['gold_volume'][tick] += volume
                series[f'trades_{agent.kind}'][tick] += 1
                series[f'volume_{agent.kind}'][tick] += volume
                group = 'noise' if agent.kind == 'N' else 'strategy'
                series[f'{group}_flow'][tick] += float(agent.account.foreign - before_foreign)
        price = float(pool.price)
        history.append(math.log(price))
        realized_return = history[-1] - current
        for agent, exposure in zip(agents, held):
            if agent.kind in ('M', 'R'):
                agent.score(realized_return, exposure)
        series['prices'][tick], series['gold'][tick] = price, float(pool.gold)
        series['pool_foreign'][tick] = float(pool.foreign)
        series['inventory_strategy'][tick] = sum(float(a.account.foreign)-a.base for a in agents if a.kind in ('M','R'))
        series['inventory_noise'][tick] = sum(float(a.account.foreign)-a.base for a in agents if a.kind == 'N')
        equities = [float(a.account.gold) + price * float(a.account.foreign) for a in agents]
        series['trend_count'][tick] = sum(a.kind == 'M' for a in agents)
        series['trend_equity_fraction'][tick] = sum(e for a, e in zip(agents, equities) if a.kind == 'M') / sum(equities)
        differences = [a.scores[0]-a.scores[1] for a in agents if a.adaptive]
        series['score_difference'][tick] = float(np.mean(differences)) if differences else 0
        series['cash_saturated'][tick] = sum(a.account.gold < Decimal('.000001') for a in agents)
        series['foreign_saturated'][tick] = sum(a.account.foreign < Decimal('.000001') for a in agents)
        if tick > cfg['burn_in']:
            score_ss += sum(float(d*d) for d in differences)
            score_n += len(differences)
    conservation = (pool.gold + pool.fees_gold + sum(a.account.gold for a in agents) == initial_gold and
                    pool.foreign + pool.fees_foreign + sum(a.account.foreign for a in agents) == initial_foreign)
    assert conservation and all(a.account.gold >= 0 and a.account.foreign >= 0 for a in agents)
    assert np.isfinite(series['prices']).all() and (series['prices'] > 0).all()
    np.testing.assert_allclose(series['pool_foreign'], cfg['foreign']-series['inventory_strategy']-series['inventory_noise'],
                               rtol=0, atol=1e-8)
    for group in ('strategy', 'noise'):
        np.testing.assert_allclose(np.cumsum(series[f'{group}_flow']), series[f'inventory_{group}'], rtol=0, atol=1e-8)
    summary = dict(conservation=conservation, switches=sum(a.switches for a in agents),
        choice_opportunities=sum(a.opportunities for a in agents), score_square_sum=score_ss, score_samples=score_n,
        score_difference_rms=math.sqrt(score_ss/score_n) if score_n else 0, budget_clipped_orders=int(budget_clips),
        trades=int(series['trades'].sum()), gold_volume=float(series['gold_volume'].sum()),
        range_pct=float((series['prices'].max()/series['prices'].min()-1)*100),
        accounts=[dict(actor=a.account.name, kind=a.kind, adaptive=a.adaptive, gamma=a.gamma,
          gold=str(a.account.gold), foreign=str(a.account.foreign), switches=a.switches,
          final_equity=equities[i], scores=a.scores.tolist()) for i, a in enumerate(agents)])
    return series, summary


def diagnostics(series, burn):
    r = np.diff(np.log(series['prices']))[burn:]
    absolute = acf(abs(r), 5000)
    return dict(returns=describe(r), nonzero_returns=describe(r[r != 0]),
        absolute_acf=absolute.tolist(), signed_acf=acf(r, 5000).tolist(), squared_acf=acf(r*r, 5000).tolist(),
        abs_acf_bands={f'{lo}-{hi}':float(np.mean(absolute[lo:hi+1])) for lo, hi in
                       [(1,1),(2,10),(11,30),(31,100),(101,300),(301,1000),(1001,5000)]},
        aggregation={str(w): describe(r[:len(r)//w*w].reshape(-1, w).sum(axis=1)) for w in [1,5,30,60,240,1000]},
        dfa={name: dict(curve=dfa(values, SIZES), fits=fits(dfa(values, SIZES)),
                       dfa2_fits=fits(dfa(values, SIZES, order=2))) for name, values in
             [('returns', r), ('absolute_returns', abs(r))]})


def run_one(task):
    cfg, mode, seed, beta, output = task
    start = perf_counter()
    directory = Path(output)/f'{mode}-seed{seed}'
    directory.mkdir(parents=True, exist_ok=True)
    series, summary = simulate(cfg, mode, seed, beta)
    diag = diagnostics(series, cfg['burn_in'])
    np.savez_compressed(directory/'series.npz', **series)
    # Wrapper and production math are both named amm.py; retain both identities.
    hashes = {str(p.relative_to(ROOT.parent.parent)):hashlib.sha256(p.read_bytes()).hexdigest() for p in
              [ROOT/'switching.py', ROOT/'switching_run.py', ROOT/'amm.py', ROOT/'diagnostics.py', ROOT/'hurst.py',
               ROOT.parent.parent/'backend/app/services/fx/amm.py', ROOT.parent.parent/'backend/app/services/fx/quantize.py']}
    result = dict(status='complete', mode=mode, seed=seed, beta=beta, config=cfg,
                  source_hashes=hashes, simulation=summary, diagnostics=diag, elapsed_seconds=perf_counter()-start)
    (directory/'result.json').write_text(json.dumps(result, indent=2)+'\n')
    print(f'{mode} seed{seed}: {summary["trades"]} trades, {summary["switches"]} switches, {result["elapsed_seconds"]:.1f}s', flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT/'results/switching-v2')
    parser.add_argument('--workers', type=int, default=3)
    args = parser.parse_args()
    cfg = read_config(args.config)
    args.output.mkdir(parents=True, exist_ok=True)
    # Calibrate only on fixed A, without selecting by volatility diagnostics.
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        fixed = list(executor.map(run_one, [(cfg, 'fixed', s, 0, str(args.output)) for s in cfg['seeds']]))
        total = sum(r['simulation']['score_samples'] for r in fixed)
        rms = math.sqrt(sum(r['simulation']['score_square_sum'] for r in fixed)/total)
        if rms <= 0:
            raise ValueError('No score contrast in fixed A; cannot calibrate beta')
        beta = 1/rms
        (args.output/'calibration.json').write_text(json.dumps(dict(method='1 / pooled fixed-A score-difference RMS',
                                              rms=rms, beta=beta, seeds=cfg['seeds']), indent=2)+'\n')
        other = list(executor.map(run_one, [(cfg, mode, s, beta if mode == 'performance' else 0, str(args.output))
                                for mode in MODES[1:] for s in cfg['seeds']]))
    # Reproducible paired-input evidence, including actual Gaussian demands.
    for seed in cfg['seeds']:
        with np.load(args.output/f'fixed-seed{seed}/series.npz') as a:
            for mode in ('random', 'performance', 'fixed_m4', 'fixed_m12', 'matched_noise'):
                with np.load(args.output/f'{mode}-seed{seed}/series.npz') as b:
                    for key in ('arrivals', 'noise_demand'):
                        np.testing.assert_array_equal(a[key], b[key])
    previous = ROOT/'results/switching'
    identical = []
    for mode in ('fixed', 'random', 'noise'):
        for seed in cfg['seeds']:
            old_path = previous/f'{mode}-seed{seed}/series.npz'
            if old_path.exists():
                with np.load(old_path) as old, np.load(args.output/f'{mode}-seed{seed}/series.npz') as new:
                    for key in old.files:
                        if key != 'score_difference':
                            np.testing.assert_array_equal(old[key], new[key])
                identical.append(f'{mode}-seed{seed}')
    (args.output/'validation.json').write_text(json.dumps(dict(paired_arrivals_and_noise=True,
              previous_market_series_identical=identical, inventory_decomposition=True), indent=2)+'\n')
    (args.output/'summary.json').write_text(json.dumps(dict(beta=beta, paired_arrivals_and_noise=True,
        runs=[dict(mode=r['mode'], seed=r['seed'], **{k:v for k,v in r['simulation'].items() if k != 'accounts'},
                   returns=r['diagnostics']['returns'], abs_acf_bands=r['diagnostics']['abs_acf_bands'],
                   dfa={k:v['fits'] for k,v in r['diagnostics']['dfa'].items()}) for r in fixed+other]), indent=2)+'\n')


if __name__ == '__main__':
    main()
