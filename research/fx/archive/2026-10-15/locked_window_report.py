"""Summarize the approved trend-window x mean-window locked-inventory grid."""

import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

from .diagnostics import describe
from .experiments import expand
from .hurst import dfa, fits

ROOT = Path('research/fx/results/position-locked-window-grid')
OUT = Path('research/fx/figures/position-locked-window-grid')
CONFIG = Path('research/fx/configs/position-locked-window-grid.json')
SIZES = [16,24,32,48,64,96,128,192,256,384,512,768,1024,1536,2048,3072,4096,6144,8192]
BANDS = [(1,1),(2,10),(11,30),(31,100),(101,300),(301,1000),(1001,5000)]


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    configs = expand(json.loads(CONFIG.read_text()))
    rows, curves = [], []
    references = {}
    for cfg in configs:
        directory = ROOT/cfg.identifier()
        result = json.loads((directory/'result.json').read_text())
        assert result['status']=='complete', (cfg.identifier(),result['status'])
        simulation = result['simulation']
        assert simulation['model_valid'] and simulation['rejected_orders']==0
        diagnostics = json.loads((directory/'diagnostics.json').read_text())
        with np.load(directory/'series.npz') as saved:
            data = {key:saved[key] for key in saved.files}
        if cfg.seed in references:
            np.testing.assert_array_equal(data['arrivals'],references[cfg.seed])
        else:
            references[cfg.seed] = data['arrivals']
        if cfg.trend_window==30 and cfg.mean_window==30:
            with np.load(Path('research/fx/results/position-locked-comparison')/cfg.identifier()/'series.npz') as old:
                for key in data:
                    np.testing.assert_array_equal(data[key],old[key])
        activity = simulation['locked_activity']
        values = np.diff(np.log(data['prices']))[cfg.burn_in:]
        row = dict(id=cfg.identifier(),seed=cfg.seed,trend_window=cfg.trend_window,mean_window=cfg.mean_window,
            range_pct=simulation['range_pct'],trades=simulation['trades'],gold_volume=simulation['actual_gold_volume'],
            peak_total_position=float(simulation['max_abs_total_foreign_position']),
            mean_hold_ticks=activity['mean_completed_hold_ticks'],median_hold_ticks=activity['median_completed_hold_ticks'],
            p90_hold_ticks=activity['p90_completed_hold_ticks'],open_positions=activity['open_positions'],
            nonzero_excess_kurtosis=describe(values[values!=0])['excess_kurtosis'],**result['metrics'])
        for name in ('trend','reversion','noise'):
            row[f'{name}_std'] = simulation['factors']['all_updates'][name]['std']
        for lo,hi in BANDS:
            row[f'abs_acf_{lo}_{hi}'] = float(np.mean(diagnostics['correlations']['absolute'][lo:hi+1]))
        statistics = {}
        for name,series in (('returns',values),('absolute_returns',abs(values))):
            curve = dfa(series,SIZES)
            statistics[name] = dict(curve=curve,dfa1_fits=fits(curve),dfa2_fits=fits(dfa(series,SIZES,order=2)))
            for scale in ('32-256','512-8192','2048-8192'):
                row[f'{name}_dfa1_{scale}'] = statistics[name]['dfa1_fits'][scale]['alpha']
                row[f'{name}_dfa2_{scale}'] = statistics[name]['dfa2_fits'][scale]['alpha']
        rows.append(row)
        curves.append(dict(id=cfg.identifier(),seed=cfg.seed,trend_window=cfg.trend_window,
            mean_window=cfg.mean_window,statistics=statistics,
            absolute_acf=diagnostics['correlations']['absolute'],
            group_factors=simulation['group_factors']))
    trends = sorted({c.trend_window for c in configs})
    means = sorted({c.mean_window for c in configs})
    groups = []
    for trend in trends:
        for mean in means:
            selected = [r for r in rows if (r['trend_window'],r['mean_window'])==(trend,mean)]
            averages = {k:float(np.mean([r[k] for r in selected])) for k in selected[0]
                        if k not in ('seed','trend_window','mean_window') and isinstance(selected[0][k],(int,float))}
            ranges = {k:[float(min(r[k] for r in selected)),float(max(r[k] for r in selected))]
                      for k in averages}
            groups.append(dict(trend_window=trend,mean_window=mean,seeds=[r['seed'] for r in selected],
                               averages=averages,seed_ranges=ranges))
    (OUT/'statistics.json').write_text(json.dumps(dict(runs=rows,groups=groups,curves=curves,
        validation=dict(complete_runs=len(rows),paired_arrivals_all_windows=True,
                        old_L30_M30_series_identical=True)),indent=2)+'\n')
    with (OUT/'runs.csv').open('w') as handle:
        writer = csv.DictWriter(handle,list(rows[0]))
        writer.writeheader();writer.writerows(rows)

    fields = [('range_pct','Full-run price range (%)'),('median_hold_ticks','Median completed hold (ticks)'),
              ('abs_acf_101_300','Mean |r| ACF, lags 101-300'),('returns_dfa1_512-8192','Return DFA1 slope, 512-8192'),
              ('absolute_returns_dfa1_512-8192','|r| DFA1 slope, 512-8192'),
              ('nonzero_excess_kurtosis','Nonzero return excess kurtosis')]
    fig,axes = plt.subplots(2,3,figsize=(14,7),constrained_layout=True)
    for ax,(field,title) in zip(axes.flat,fields):
        matrix = np.array([[next(g for g in groups if (g['trend_window'],g['mean_window'])==(t,m))['averages'][field]
                            for m in means] for t in trends])
        im = ax.imshow(matrix,aspect='auto',cmap='viridis')
        ax.set(xticks=range(len(means)),xticklabels=means,yticks=range(len(trends)),yticklabels=trends,
               xlabel='Mean window (ticks)',ylabel='Trend window (ticks)',title=title)
        for i in range(len(trends)):
            for j in range(len(means)):
                ax.text(j,i,f'{matrix[i,j]:.3g}',ha='center',va='center',color='white',fontsize=9,
                        bbox=dict(facecolor='black',alpha=.25,edgecolor='none',pad=1))
        fig.colorbar(im,ax=ax,shrink=.7)
    fig.savefig(OUT/'grid.png',dpi=150);plt.close(fig)
    palette = ['#64748b','#2563eb','#d97706','#16a34a','#9333ea']
    fig,axes = plt.subplots(1,len(trends),figsize=(15,4),constrained_layout=True)
    for ax,trend in zip(axes,trends):
        for mean,color in zip(means,palette):
            selected = [r for r in curves if (r['trend_window'],r['mean_window'])==(trend,mean)]
            averaged = np.mean([r['absolute_acf'] for r in selected],axis=0)
            ax.semilogx(np.arange(1,len(averaged)),averaged[1:],color=color,lw=.8,label=f'M={mean}')
        ax.set(title=f'Trend window {trend}',xlabel='Lag (ticks)',ylabel='Mean |r| autocorrelation')
        ax.axhline(0,color='#94a3b8',ls='--',lw=.8);ax.legend(fontsize=8)
    fig.savefig(OUT/'acf.png',dpi=150);plt.close(fig)
    fig,axes = plt.subplots(2,len(trends),figsize=(15,8),constrained_layout=True)
    for column,trend in enumerate(trends):
        for mean,color in zip(means,palette):
            selected = [r for r in curves if (r['trend_window'],r['mean_window'])==(trend,mean)]
            for ax,name in zip(axes[:,column],('returns','absolute_returns')):
                values = [r['statistics'][name]['curve'] for r in selected]
                averaged = np.exp(np.mean([[np.log(p['F']) for p in curve] for curve in values],axis=0))
                ax.loglog([p['size'] for p in values[0]],averaged,color=color,lw=.9,label=f'M={mean}')
                ax.set(title=f'{name}; trend window {trend}',xlabel='DFA scale (ticks)',ylabel='Geometric mean F')
                ax.legend(fontsize=8)
    fig.savefig(OUT/'dfa.png',dpi=150);plt.close(fig)
    for group in groups:
        a = group['averages']
        print(group['trend_window'],group['mean_window'],{k:round(a[k],5) for k in
              ('range_pct','median_hold_ticks','abs_acf_101_300','abs_acf_301_1000','absolute_returns_dfa1_512-8192',
               'returns_dfa1_512-8192','excess_kurtosis','nonzero_excess_kurtosis')})


if __name__=='__main__':
    main()
