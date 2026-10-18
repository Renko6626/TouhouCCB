"""Export bounded-position grids and inspect finite-scale cycles descriptively."""

import argparse
import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from scipy.signal import welch


def spectrum(prices, burn):
    # Returns, not price levels: price-level low-frequency power can reflect drift.
    returns = np.diff(np.log(prices))[burn:]
    frequency, power = welch(returns, nperseg=8192, detrend='constant')
    power[0] = 0
    peak = int(np.argmax(power))
    total = power.sum()
    return dict(spectral_peak_period_ticks=float(1/frequency[peak]) if peak and total else None,
                spectral_peak_three_bin_share=float(power[max(1, peak-1):peak+2].sum()/total) if total else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('groups', nargs='+', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    rows = []
    locations = {}
    for group in args.groups:
        for path in sorted(group.glob('*/result.json')):
            result = json.loads(path.read_text())
            config = result['metadata']['config']
            row = dict(group=group.name, id=result['id'], status=result['status'], **config)
            if 'simulation' in result:
                simulation = result['simulation']
                for key in ('range_pct', 'min_price', 'max_price', 'final_price', 'actual_gold_volume',
                            'trades', 'arrivals', 'max_abs_foreign_position', 'max_abs_total_foreign_position',
                            'fees_foreign', 'pool_foreign_final', 'rejected_orders'):
                    row[key] = simulation[key]
                for scope, factors in simulation['factors'].items():
                    for name, values in factors.items():
                        row[f'{scope}_{name}_std'] = values['std']
            row.update(result.get('metrics', {}))
            if result['status'] == 'complete':
                with np.load(path.parent/'series.npz') as series:
                    row.update(spectrum(series['prices'], config['burn_in']))
            rows.append(row)
            locations[(group.name, result['id'])] = path.parent
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with (args.output/'grid.csv').open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader(); writer.writerows(rows)
    (args.output/'grid.json').write_text(json.dumps(rows, indent=2)+'\n')

    factors = [r for r in rows if r['group'] == 'position-factor-grid' and r['status'] == 'complete']
    if factors:
        fig, axes = plt.subplots(3, 3, figsize=(12, 10), constrained_layout=True)
        for i, noise in enumerate((.001, .005, .02)):
            for j, window in enumerate((30, 100, 300)):
                ax = axes[i, j]
                subset = [r for r in factors if r['noise_sigma'] == noise and r['trend_window'] == window]
                for b, color in zip((0, .1, 1), ('#64748b', '#2563eb', '#d97706')):
                    selected = sorted((r for r in subset if r['reversion_strength'] == b), key=lambda r:r['trend_strength'])
                    ax.plot([r['trend_strength'] for r in selected], [r['return_acf1'] for r in selected], 'o-', label=f'b={b}', color=color)
                ax.axhline(0, color='#cbd5e1', linewidth=.6)
                ax.set(title=f'noise={noise}; window={window}', xlabel='Trend coefficient a', ylabel='Return ACF lag 1')
                ax.legend(fontsize=8)
        fig.savefig(args.output/'trend-feedback.png', dpi=150); plt.close(fig)

    # Match explicit parameter tuples so the figure is reproducible, not top-ranked noise.
    chosen = [(0, .1, .005, 30), (3, .1, .005, 30), (10, .1, .005, 30),
              (30, .1, .005, 30), (30, 1, .005, 30), (30, .1, .005, 300)]
    available = [next((r for r in factors if (r['trend_strength'], r['reversion_strength'], r['noise_sigma'], r['trend_window']) == c), None) for c in chosen]
    if all(available):
        fig, axes = plt.subplots(6, 2, figsize=(12, 16), constrained_layout=True)
        for i, row in enumerate(available):
            folder = locations[(row['group'], row['id'])]
            with np.load(folder/'series.npz') as series:
                prices = series['prices']
                start = row['burn_in']; stop = min(start+4000, len(prices))
                axes[i, 0].plot(np.arange(start, stop), prices[start:stop], linewidth=.7)
            diagnostics = json.loads((folder/'diagnostics.json').read_text())
            for key, label in [('returns', 'Returns'), ('normalized_absolute', '|Normalized returns|')]:
                values = diagnostics['correlations'][key][:1001]
                axes[i, 1].plot(np.arange(len(values)), values, label=label, linewidth=.8)
            axes[i, 0].set(title=f"a={row['trend_strength']}, b={row['reversion_strength']}, L={row['trend_window']}", xlabel='Tick', ylabel='Gold / foreign')
            axes[i, 1].set(xlabel='Lag (ticks)', ylabel='ACF'); axes[i, 1].legend(fontsize=8)
        fig.savefig(args.output/'prices-and-correlations.png', dpi=150); plt.close(fig)
    candidate = sorted((r for r in rows if r['group'] == 'position-grid-confirmation'
                        and r['status'] == 'complete' and r['position_cap'] == 250), key=lambda r:r['seed'])
    if candidate:
        fig, axes = plt.subplots(len(candidate), 2, figsize=(12, 3*len(candidate)), squeeze=False, constrained_layout=True)
        for i, row in enumerate(candidate):
            folder = locations[(row['group'], row['id'])]
            with np.load(folder/'series.npz') as series:
                start = row['burn_in']; stop = start+20000
                axes[i, 0].plot(np.arange(start, stop), series['prices'][start:stop], linewidth=.7)
            diagnostics = json.loads((folder/'diagnostics.json').read_text())
            for key, label in [('returns', 'Returns'), ('normalized_absolute', '|Normalized returns|')]:
                values = diagnostics['correlations'][key][1:5001]
                axes[i, 1].plot(np.arange(1, len(values)+1), values, label=label, linewidth=.7)
            axes[i, 0].set(title=f"Cap 250, adjustment 0.2, seed {row['seed']}", xlabel='Tick', ylabel='Gold / foreign')
            axes[i, 1].set(xlabel='Lag (ticks)', ylabel='ACF'); axes[i, 1].legend(fontsize=8)
        fig.savefig(args.output/'candidate-persistence.png', dpi=150); plt.close(fig)
    print(f'Exported {len(rows)} runs to {args.output}')


if __name__ == '__main__':
    main()
