"""Run existing scientific diagnostics on saved paired reversion experiments."""

import argparse
import csv
import hashlib
import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import scipy

from .study import analyze


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=Path('research/fx/results/reversion-comparison'))
    parser.add_argument('--bootstrap', type=int, default=200)
    args = parser.parse_args()
    if args.bootstrap < 1:
        parser.error('bootstrap must be positive')
    output = args.input/'metrics'
    output.mkdir(exist_ok=True)
    rows = json.loads((args.input/'summary.json').read_text())
    simulation_params = json.loads((args.input/'parameters.json').read_text())
    burn_in = simulation_params['burn_in']
    metadata = dict(bootstrap=args.bootstrap, burn_in=burn_in, tail_minimum=500,
                    thinned_interval=60, thinned_tail_minimum=30,
                    normalization='r * sqrt(P_previous/5): approximate constant-k reserve ratio',
                    numpy=np.__version__, scipy=scipy.__version__,
                    source_hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest()
                                   for p in Path(__file__).parent.glob('*.py')},
                    input_hashes={str(p.relative_to(args.input)):hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in args.input.glob('seed-*/series.npz')})
    (output/'parameters.json').write_text(json.dumps(metadata, indent=2))
    results = {}; summary = []
    for row in rows:
        seed, strength = row['seed'], row['reversion_strength']
        key = f'seed-{seed}-b-{strength:g}'
        print(f'analyze {key}', flush=True)
        prices = np.load(args.input/key/'series.npz')['prices']
        returns = np.diff(np.log(prices))[burn_in:]
        normalized = returns*np.sqrt(prices[:-1][burn_in:]/5)
        result = analyze(returns, normalized, args.bootstrap, seed, thin_min_tail=30)
        results[key] = result
        fit = result['pareto']
        summary.append(dict(seed=seed, reversion_strength=strength,
                            std=result['distribution']['std'],
                            excess_kurtosis=result['distribution']['excess_kurtosis'],
                            tail_beta=fit['beta'], tail_n=fit['n_tail'], tail_decades=fit['tail_decades'],
                            p_iid=fit['gof']['p_iid'], thin_p_iid=result['thinned_pareto']['gof']['p_iid'],
                            return_acf1=result['correlations']['returns'][1],
                            normalized_abs_acf100_1000=float(np.mean(result['correlations']['normalized_absolute'][100:1001])),
                            rs_normalized_q20=result['memory']['normalized']['20']['scaling_slope'],
                            rs_shuffled_q20=result['memory']['block_shuffled']['20']['scaling_slope']))
        (output/'diagnostics.json').write_text(json.dumps(results, indent=2))
        print(f"done: beta={fit['beta']:.2f}, p={fit['gof']['p_iid']:.4f}", flush=True)
    with (output/'summary.csv').open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary[0]))
        writer.writeheader(); writer.writerows(summary)

    seeds = list(dict.fromkeys(row['seed'] for row in rows))
    strengths = list(dict.fromkeys(row['reversion_strength'] for row in rows))
    figures(output, results, seeds, strengths)
    print(f'saved {output}',flush=True)


def figures(output, results, seeds, strengths):
    colors = plt.cm.Blues(np.linspace(.5,.95,len(strengths)))
    fig, axes = plt.subplots(len(seeds),3,figsize=(14,3.4*len(seeds)),squeeze=False,constrained_layout=True)
    for index, seed in enumerate(seeds):
        for strength, color in zip(strengths, colors):
            key = f'seed-{seed}-b-{strength:g}'
            result = results[key]; label=f'b={strength:g}'
            hill = result['hill']
            axes[index,0].plot([float(q)*100 for q in hill],list(hill.values()),'o-',color=color,label=label)
            ac = result['correlations']['normalized_absolute']
            axes[index,1].plot(np.arange(1,1001),ac[1:1001],color=color,alpha=.8,label=label)
            for name, style in [('normalized','-'),('block_shuffled','--')]:
                curve=result['memory'][name]['20']
                axes[index,2].loglog(curve['sizes'],curve['statistic'],style,color=color,label=f'{label} {name}')
        axes[index,0].set(title=f'Seed {seed}: Hill CCDF exponent',xlabel='Threshold percentile',ylabel='Beta')
        axes[index,1].set(title='Pool-depth normalized |return| ACF',xlabel='Lag (tick)',ylabel='ACF')
        axes[index,1].axhline(0,color='gray',lw=.5)
        axes[index,2].set(title='Modified R/S q=20; dashed: shuffled',xlabel='Block size',ylabel='Median Q')
        axes[index,2].set_xticks([128,512,2048,4096],labels=['128','512','2048','4096'])
        axes[index,2].set_xticks([],minor=True)
        for axis in axes[index]:
            axis.legend(fontsize=8); axis.spines[['top','right']].set_visible(False)
    fig.savefig(output/'diagnostics.png',dpi=150); plt.close(fig)


if __name__=='__main__':
    main()
