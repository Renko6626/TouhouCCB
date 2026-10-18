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
from .switching import Agent, execute
from .diagnostics import acf, describe
from .hurst import dfa, fits

ROOT = Path(__file__).resolve().parent
SIZES = [16,24,32,48,64,96,128,192,256,384,512,768,1024,1536,2048,3072,4096,6144,8192]


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
    pool = Pool(Decimal(str(cfg['gold'])), Decimal(str(cfg['foreign'])), Decimal(str(cfg['fee_rate'])))
    p0 = float(pool.price)
    kinds = ['M'] * cfg['type_counts'][0] + ['R'] * cfg['type_counts'][1] + ['N'] * cfg['type_counts'][2]
    if mode == 'noise':
        kinds = ['N'] * cfg['bots']
    seen = {'M': 0, 'R': 0, 'N': 0}
    agents = []
    for i, kind in enumerate(kinds):
        adaptive = kind != 'N' and seen[kind] < cfg['adaptive_per_type']
        seen[kind] += 1
        agents.append(Agent(i, kind, adaptive, seed, cfg, p0))
    n = cfg['ticks']
    # Precomputed exogenous demand, independent of executed trades and choices.
    schedules = [np.random.default_rng(seed*1000003+i*101+1).poisson(cfg['rate'], n) for i in range(len(agents))]
    noise_rngs = [np.random.default_rng(seed*1000003+i*101+2) for i in range(len(agents))]
    execution_rng = random.Random(seed*1000003+4)
    series = {k: np.zeros(n+1) for k in ('prices', 'gold', 'arrivals', 'noise_demand', 'trades',
               'gold_volume', 'trend_count', 'trend_equity_fraction', 'score_difference', 'cash_saturated', 'foreign_saturated')}
    series['prices'][0], series['gold'][0] = p0, cfg['gold']
    series['trend_count'][0] = kinds.count('M')
    series['trend_equity_fraction'][0] = kinds.count('M') / len(agents)
    history = [math.log(p0)]
    initial_gold = pool.gold + sum(a.account.gold for a in agents)
    initial_foreign = pool.foreign + sum(a.account.foreign for a in agents)
    score_ss = score_n = 0
    budget_clips = 0
    for tick in range(1, n+1):
        current = history[-1]
        ret = current - history[-2] if len(history) > 1 else 0.
        for agent in agents:
            if agent.kind != 'N':
                agent.observe(current, history, ret)
                agent.reconsider(mode, beta)
        events = [i for i, row in enumerate(schedules) for _ in range(row[tick-1])]
        execution_rng.shuffle(events)
        series['arrivals'][tick] = len(events)
        for i in events:
            agent = agents[i]
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
                series['gold_volume'][tick] += float(quote.input_amount if wanted > 0 else quote.output_amount)
        price = float(pool.price)
        history.append(math.log(price))
        series['prices'][tick], series['gold'][tick] = price, float(pool.gold)
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
    parser.add_argument('--output', type=Path, default=ROOT/'results/switching')
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
                                for mode in ('random', 'performance', 'noise') for s in cfg['seeds']]))
    # Reproducible paired-input evidence, including actual Gaussian demands.
    for seed in cfg['seeds']:
        with np.load(args.output/f'fixed-seed{seed}/series.npz') as a:
            for mode in ('random', 'performance'):
                with np.load(args.output/f'{mode}-seed{seed}/series.npz') as b:
                    for key in ('arrivals', 'noise_demand'):
                        np.testing.assert_array_equal(a[key], b[key])
    (args.output/'summary.json').write_text(json.dumps(dict(beta=beta, paired_arrivals_and_noise=True,
        runs=[dict(mode=r['mode'], seed=r['seed'], **{k:v for k,v in r['simulation'].items() if k != 'accounts'},
                   returns=r['diagnostics']['returns'], abs_acf_bands=r['diagnostics']['abs_acf_bands'],
                   dfa={k:v['fits'] for k,v in r['diagnostics']['dfa'].items()}) for r in fixed+other]), indent=2)+'\n')


if __name__ == '__main__':
    main()
