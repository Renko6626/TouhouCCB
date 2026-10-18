"""Small paired report from saved switching runs; no parameter selection."""
import argparse
import csv
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from .diagnostics import acf, describe

MODES = ['fixed', 'random', 'performance', 'noise']
COLORS = ['#64748b', '#d97706', '#2563eb', '#16a34a']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parent/'results/switching')
    args = parser.parse_args()
    summary = json.loads((args.source/'summary.json').read_text())
    seeds = sorted({r['seed'] for r in summary['runs']})
    rows, saved, results = [], {}, {}
    for mode in MODES:
        for seed in seeds:
            directory = args.source/f'{mode}-seed{seed}'
            result = json.loads((directory/'result.json').read_text())
            with np.load(directory/'series.npz') as raw:
                saved[mode, seed] = {key:raw[key] for key in raw.files}
            results[mode, seed] = result
            sim, diag = result['simulation'], result['diagnostics']
            x = saved[mode, seed]
            burn = result['config']['burn_in']
            returns = np.diff(np.log(x['prices']))[burn:]
            row = dict(mode=mode, seed=seed, price_range_pct=sim['range_pct'], trades=sim['trades'],
                       switches=sim['switches'], choices=sim['choice_opportunities'], gold_volume=sim['gold_volume'],
                       excess_kurtosis=diag['returns']['excess_kurtosis'],
                       nonzero_excess_kurtosis=diag['nonzero_returns']['excess_kurtosis'],
                       zero_fraction=diag['returns']['zero_fraction'],
                       trend_count_min=int(x['trend_count'].min()), trend_count_max=int(x['trend_count'].max()),
                       cash_saturation_tick_fraction=float(np.mean(x['cash_saturated'][burn+1:] > 0)),
                       foreign_saturation_tick_fraction=float(np.mean(x['foreign_saturated'][burn+1:] > 0)),
                       **{f'abs_acf_{k}':v for k,v in diag['abs_acf_bands'].items()})
            for name in ('returns', 'absolute_returns'):
                for band, fit in diag['dfa'][name]['fits'].items():
                    row[f'{name}_dfa1_{band}'] = fit['alpha']
            rows.append(row)
            blocks = []
            for start in range(0, len(returns)-9999, 10000):
                block = returns[start:start+10000]
                curve = acf(abs(block), 1000)
                blocks.append(dict(start_return_tick=burn+start+1, end_return_tick=burn+start+10000,
                                   abs_acf_101_1000=float(curve[101:1001].mean()), **describe(block)))
            (directory/'blocks.json').write_text(json.dumps(blocks, indent=2)+'\n')
    with (args.source/'comparison.csv').open('w') as handle:
        writer = csv.DictWriter(handle, list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    means = {mode:{key:float(np.mean([r[key] for r in rows if r['mode'] == mode]))
                         for key in rows[0] if key not in ('mode', 'seed')} for mode in MODES}
    (args.source/'comparison.json').write_text(json.dumps(dict(runs=rows, means=means), indent=2)+'\n')

    fig, axes = plt.subplots(len(seeds), 3, figsize=(15, 3*len(seeds)), squeeze=False, constrained_layout=True)
    for i, seed in enumerate(seeds):
        for mode, color in zip(MODES, COLORS):
            x = saved[mode, seed]
            r = np.diff(np.log(x['prices']))
            count = len(r)//100
            vol = r[:count*100].reshape(-1,100).std(axis=1)
            axes[i,0].plot(np.arange(len(x['prices']))[::20], x['prices'][::20], lw=.65, color=color, label=mode)
            axes[i,1].plot((np.arange(count)+.5)*100, vol, lw=.6, color=color, label=mode)
            if mode != 'noise':
                axes[i,2].plot(np.arange(len(x['trend_count']))[::20], x['trend_count'][::20], lw=.65, color=color, label=mode)
        for ax, title in zip(axes[i], ['Price', '100-tick return standard deviation', 'Number using trend rule']):
            ax.set(title=f'Seed {seed}: {title}', xlabel='Tick')
            ax.legend(fontsize=7)
    fig.savefig(args.source/'paths.png', dpi=140)
    plt.close(fig)
    fig, axes = plt.subplots(1,3,figsize=(15,4),constrained_layout=True)
    for mode, color in zip(MODES, COLORS):
        absolute = np.mean([results[mode,s]['diagnostics']['absolute_acf'] for s in seeds], axis=0)
        axes[0].semilogx(np.arange(1,len(absolute)), absolute[1:], color=color, lw=.8, label=mode)
        aggregation = [results[mode,s]['diagnostics']['aggregation'] for s in seeds]
        widths = list(aggregation[0])
        kurtosis = [np.mean([a[w]['excess_kurtosis'] for a in aggregation]) for w in widths]
        axes[1].semilogx([int(w) for w in widths], kurtosis, 'o-', color=color, lw=.8, label=mode)
        curves = [results[mode,s]['diagnostics']['dfa']['absolute_returns']['curve'] for s in seeds]
        f = np.exp(np.mean([[np.log(p['F']) for p in c] for c in curves], axis=0))
        axes[2].loglog([p['size'] for p in curves[0]], f, 'o-', color=color, lw=.8, label=mode)
    for ax, title in zip(axes, ['Mean absolute-return ACF', 'Mean aggregation excess kurtosis', 'Absolute-return DFA1 (geometric mean)']):
        ax.set(title=title, xlabel='Ticks')
        ax.legend(fontsize=8)
    axes[0].axhline(0, color='#94a3b8', ls='--', lw=.7)
    fig.savefig(args.source/'diagnostics.png', dpi=140)
    plt.close(fig)
    for mode, row in means.items():
        print(mode, json.dumps({key:round(row[key],5) for key in
              ['switches', 'price_range_pct', 'excess_kurtosis', 'abs_acf_31-100', 'abs_acf_101-300', 'abs_acf_301-1000',
               'returns_dfa1_512-8192', 'absolute_returns_dfa1_512-8192']}))


if __name__ == '__main__':
    main()
