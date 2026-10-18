"""Read the six approved runs; compare execution and finite-scale statistics."""

import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

from .experiments import expand
from .hurst import dfa, fits
from .diagnostics import acf, describe

ROOT = Path('research/fx/results/position-locked-comparison')
OUT = Path('research/fx/figures/position-locked-comparison')
SIZES = [16,24,32,48,64,96,128,192,256,384,512,768,1024,1536,2048,3072,4096,6144,8192]


def scale_details(result, data):
    cfg = result['metadata']['config']
    values = np.diff(np.log(data['prices']))[cfg['burn_in']:]
    diagnostics = json.loads((ROOT/result['id']/'diagnostics.json').read_text())
    correlations = np.asarray(diagnostics['correlations']['absolute'])
    bands = [(1,1),(2,10),(11,30),(31,100),(101,300),(301,1000),(1001,5000)]
    row = dict(strategy=cfg['strategy'],seed=cfg['seed'],
        lag_bands={f'{lo}-{hi}':float(correlations[lo:hi+1].mean()) for lo,hi in bands},
        nonzero_excess_kurtosis=describe(values[values!=0])['excess_kurtosis'],
        aggregation_excess_kurtosis={k:v['excess_kurtosis'] for k,v in diagnostics['aggregation'].items()},
        blocks=[])
    # Fixed complete blocks after burn-in; the last 5000 ticks are not a full block.
    for start in range(0,len(values)-9999,10000):
        block = values[start:start+10000]
        curve = acf(abs(block),1000)
        row['blocks'].append(dict(start=cfg['burn_in']+start,end=cfg['burn_in']+start+10000,
            abs_acf1=float(curve[1]),abs_acf100_1000=float(curve[100:1001].mean()),
            excess_kurtosis=describe(block)['excess_kurtosis'],std=float(block.std())))
    return row, correlations


def plot_scales(details, correlations):
    colors = {'position':'#64748b','locked_position':'#2563eb'}
    fig, axes = plt.subplots(1,2,figsize=(12,4),constrained_layout=True)
    for row, curve in zip(details,correlations):
        name = f"{row['strategy']} seed {row['seed']}"
        axes[0].semilogx(np.arange(1,1001),curve[1:1001],color=colors[row['strategy']],
                        lw=.6,alpha=.65,label=name)
        values = row['aggregation_excess_kurtosis']
        axes[1].semilogx([int(k) for k in values],list(values.values()),'o-',
                        color=colors[row['strategy']],lw=.8,markersize=3,label=name)
    axes[0].set(title='Absolute return autocorrelation',xlabel='Lag (ticks)',ylabel='Correlation')
    axes[1].set(title='Excess kurtosis of non-overlapping returns',xlabel='Aggregation width (ticks)',ylabel='Excess kurtosis')
    for ax in axes:
        ax.axhline(0,color='#94a3b8',lw=.8,linestyle='--')
        ax.legend(fontsize=7)
    fig.savefig(OUT/'correlation-kurtosis.png',dpi=150)
    plt.close(fig)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    configs = expand(json.loads(Path('research/fx/configs/position-locked-comparison.json').read_text()))
    rows = []
    curves = []
    series = {}
    details = []
    correlations = []
    for cfg in configs:
        path = ROOT / cfg.identifier()
        result = json.loads((path / 'result.json').read_text())
        assert result['status'] == 'complete', result
        assert result['simulation']['model_valid']
        with np.load(path / 'series.npz') as saved:
            data = {key: saved[key] for key in saved.files}
        series[cfg.strategy, cfg.seed] = data
        detail, correlation = scale_details(result, data)
        details.append(detail)
        correlations.append(correlation)
        simulation = result['simulation']
        row = dict(strategy=cfg.strategy, seed=cfg.seed, id=result['id'],
                   range_pct=simulation['range_pct'], trades=simulation['trades'],
                   gold_volume=simulation['actual_gold_volume'],
                   peak_individual_foreign=float(simulation['max_abs_foreign_position']),
                   peak_total_foreign=float(simulation['max_abs_total_foreign_position']),
                   **result['metrics'])
        row.update({k:v for k,v in simulation.get('locked_activity',{}).items()
                    if k in ('entries','exits','open_positions','mean_completed_hold_ticks',
                             'median_completed_hold_ticks','p90_completed_hold_ticks','max_completed_hold_ticks')})
        returns = np.diff(np.log(data['prices']))[cfg.burn_in:]
        for name, values in (('returns', returns), ('absolute_returns', abs(returns))):
            curve = dfa(values, SIZES)
            fit1 = fits(curve)
            fit2 = fits(dfa(values, SIZES, order=2))
            curves.append(dict(strategy=cfg.strategy, seed=cfg.seed, statistic=name,
                               curve=curve, dfa1_fits=fit1, dfa2_fits=fit2))
            for scale in ('32-256','512-8192','2048-8192'):
                row[f'{name}_dfa1_{scale}'] = fit1[scale]['alpha']
                row[f'{name}_dfa2_{scale}'] = fit2[scale]['alpha']
        rows.append(row)
    paired = []
    for seed in sorted({c.seed for c in configs}):
        baseline = series['position', seed]
        locked = series['locked_position', seed]
        np.testing.assert_array_equal(baseline['arrivals'], locked['arrivals'])
        old_result = json.loads((Path('research/fx/results/position-factor-mix-cap500') /
                                next(c.identifier() for c in configs if c.strategy == 'position' and c.seed == seed) /
                                'result.json').read_text())
        with np.load(Path('research/fx/results/position-factor-mix-cap500') / old_result['id'] / 'series.npz') as old:
            for key in baseline:
                np.testing.assert_array_equal(baseline[key], old[key])
        paired.append(dict(seed=seed, arrivals_identical=True, baseline_series_identical_to_old_Q500=True))
    means = []
    for strategy in ('position','locked_position'):
        selected = [r for r in rows if r['strategy'] == strategy]
        means.append(dict(strategy=strategy, **{k:float(np.mean([r[k] for r in selected]))
                     for k in selected[0] if k not in ('seed',) and isinstance(selected[0][k],(int,float))
                     and all(isinstance(r.get(k),(int,float)) for r in selected)}))
    (OUT / 'statistics.json').write_text(json.dumps(dict(runs=rows,means=means,dfa=curves,paired=paired),indent=2)+'\n')
    (OUT / 'scale-details.json').write_text(json.dumps(details,indent=2)+'\n')
    plot_scales(details,correlations)
    keys = list(dict.fromkeys(key for row in rows for key in row))
    with (OUT / 'runs.csv').open('w') as handle:
        writer = csv.DictWriter(handle, keys)
        writer.writeheader()
        writer.writerows(rows)

    fig, axes = plt.subplots(3,2,figsize=(12,9),constrained_layout=True)
    colors = {'position':'#64748b','locked_position':'#2563eb'}
    for index, seed in enumerate((41,42,43)):
        for strategy in colors:
            data = series[strategy,seed]
            price = data['prices']
            returns = np.diff(np.log(price))
            blocks = returns.reshape(-1,100).std(axis=1)
            axes[index,0].plot(np.arange(len(price))[::20],price[::20],lw=.6,
                               label=strategy,color=colors[strategy])
            axes[index,1].plot((np.arange(len(blocks))+.5)*100,blocks,lw=.6,
                               label=strategy,color=colors[strategy])
        axes[index,0].set(title=f'Seed {seed}: price',xlabel='Tick',ylabel='Price')
        axes[index,1].set(title='Non-overlapping 100-tick return std',xlabel='Tick',ylabel='Std')
        for ax in axes[index]:
            ax.legend(fontsize=8)
    fig.savefig(OUT/'paths.png',dpi=150)
    plt.close(fig)
    fig, axes = plt.subplots(1,2,figsize=(12,4),constrained_layout=True)
    for ax, statistic in zip(axes,('returns','absolute_returns')):
        for row in curves:
            if row['statistic'] != statistic:
                continue
            ax.loglog([p['size'] for p in row['curve']],[p['F'] for p in row['curve']],
                      color=colors[row['strategy']],lw=.8,alpha=.8,
                      label=f"{row['strategy']} seed {row['seed']}")
        ax.set(title=statistic,xlabel='DFA scale (ticks)',ylabel='F(scale)')
        ax.legend(fontsize=7)
    fig.savefig(OUT/'dfa.png',dpi=150)
    plt.close(fig)
    for mean in means:
        print(json.dumps(mean))
    print(json.dumps(paired))


if __name__ == '__main__':
    main()
