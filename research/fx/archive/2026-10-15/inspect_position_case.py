"""Extract a declared bounded-position run and draw focused scientific figures."""
import argparse
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from scipy import signal, stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path('research/fx/results/position-grid-confirmation/971b40e3667f012a'))
    parser.add_argument('--output', type=Path, default=Path('research/fx/figures/position-cap250-seed21'))
    args = parser.parse_args()
    result = json.loads((args.source/'result.json').read_text())
    diagnostics = json.loads((args.source/'diagnostics.json').read_text())
    assert result['status'] == 'complete'
    cfg = result['metadata']['config']; burn = cfg['burn_in']
    with np.load(args.source/'series.npz') as source:
        prices = source['prices']; gold = source['gold']; counts = source['counts']; volume = source['volume']
    raw = np.diff(np.log(prices))[burn:]
    normalized = raw*gold[:-1][burn:]/gold[0]
    start, stop = burn, burn+4000
    tick = np.arange(start+1, stop+1)
    zoom = raw[:4000]
    width = 50
    block_std = zoom.reshape(-1, width).std(axis=1, ddof=1)
    block_time = tick.reshape(-1, width).mean(axis=1)
    args.output.mkdir(parents=True, exist_ok=True)
    # Raw extraction: tick t return is log(P_t/P_(t-1)), volume/counts at t.
    extracted = np.column_stack((np.arange(start, stop+1), prices[start:stop+1],
        np.r_[np.nan, zoom], np.r_[np.nan, normalized[:4000]],
        counts[start:stop+1], volume[start:stop+1], gold[start:stop+1]))
    np.savetxt(args.output/'zoom.csv', extracted, delimiter=',',
        header='tick,price_gold_per_foreign,log_return,depth_normalized_log_return,trade_count,gold_volume,pool_gold', comments='')
    plt.rcParams.update({'font.size':11, 'axes.spines.top':False, 'axes.spines.right':False,
                         'axes.titlelocation':'left', 'axes.grid':True, 'grid.alpha':.16})
    blue, orange, grey = '#2563eb', '#d97706', '#64748b'
    fig, axes = plt.subplots(4, 1, figsize=(12, 11), constrained_layout=True)
    factor_label=' / '.join(f"{cfg['bots']//len(cfg['factor_mix'])} bots a={a:g}, b={b:g}" for a,b in cfg['factor_mix']) if cfg.get('factor_mix') else f"{cfg['bots']} bots; a={cfg['trend_strength']}, b={cfg['reversion_strength']}"
    window_label=str(cfg.get('window_mix') or cfg['trend_window'])
    fig.suptitle(f"Bounded FX inventory: cap {cfg['position_cap']}, adjustment {cfg['adjustment']:.0%}, seed {cfg['seed']}\n"
                 f"{factor_label}; noise={cfg['noise_sigma']}; window={window_label} ticks", fontsize=14)
    # Full-price bands retain every extreme; no point-sampling peak loss.
    all_prices = prices[burn:]
    chunks = [all_prices[i:i+500] for i in range(0, len(all_prices), 500)]
    centers = [burn+i*500+(len(x)-1)/2 for i,x in enumerate(chunks)]
    axes[0].fill_between(centers, [x.min() for x in chunks], [x.max() for x in chunks], color=blue, alpha=.22, label='500-tick min/max')
    axes[0].plot(centers, [x.mean() for x in chunks], color=blue, linewidth=.9, label='500-tick mean')
    axes[0].axvspan(start, stop, color=orange, alpha=.18, label='Zoom: ticks 5000-9000')
    axes[0].set(title='A. Full post-burn-in price: local swings and slow drift', ylabel='Gold / foreign', xlabel='Tick')
    axes[0].set_xlim(burn, cfg['ticks'])
    axes[0].set_xticks(np.unique(np.r_[burn, np.arange(50000, cfg['ticks']+1, 50000), cfg['ticks']]))
    axes[0].legend(fontsize=9, ncol=3)
    axes[1].plot(np.arange(start,stop+1), prices[start:stop+1], color=blue, linewidth=1)
    axes[1].set(title='B. First 4000 ticks after burn-in (fixed window)', ylabel='Gold / foreign')
    axes[2].plot(tick, zoom*100, color=blue, linewidth=.65)
    axes[2].axhline(0, color=grey, linewidth=.7)
    axes[2].set(title='C. Single-tick log returns', ylabel='Log return (%)')
    axes[3].plot(block_time, block_std*100, 'o-', color=orange, linewidth=1, markersize=3)
    axes[3].set(title='D. Volatility in disjoint 50-tick blocks (no rolling overlap)', ylabel='Return std (%)', xlabel='Tick')
    for ax in axes[1:]: ax.set_xlim(start, stop)
    fig.savefig(args.output/'trajectory.png', dpi=160)
    fig.savefig(args.output/'trajectory.svg'); plt.close(fig)

    f, power = signal.welch(normalized, nperseg=8192, detrend='constant')
    power[0] = 0; peak = int(np.argmax(power))
    share = float(power[max(1,peak-1):peak+2].sum()/power.sum())
    period = float(1/f[peak])
    fig, axes = plt.subplots(2, 2, figsize=(12, 9), constrained_layout=True)
    fig.suptitle(f'Dependence, spectrum and distribution: {len(raw):,} post-burn-in returns', fontsize=14)
    for key, color, label, linestyle in [('returns',blue,'Signed returns','-'),
                                         ('normalized_absolute',orange,'|Depth-normalized returns|','--')]:
        values = diagnostics['correlations'][key][1:5001]
        axes[0,0].plot(np.arange(1,len(values)+1), values, color=color, label=label, linewidth=1, linestyle=linestyle)
    axes[0,0].axhline(0,color=grey,linewidth=.7)
    axes[0,0].set(title='E. Full-series autocorrelation (lag 0 excluded)', xlabel='Lag (ticks)', ylabel='ACF')
    axes[0,0].legend(fontsize=9)
    mask = (f>=1/2000)&(f<=1/20)
    axes[0,1].plot(1/f[mask], power[mask]/power.sum(), color=blue, linewidth=1)
    axes[0,1].axvline(period, color=orange, linestyle='--', label=f'Peak ~{period:.0f} ticks; 3 bins {share:.1%}')
    axes[0,1].set(xscale='log',title='F. Welch spectrum of depth-normalized returns', xlabel='Period (ticks; log scale)', ylabel='Power per frequency bin / total')
    axes[0,1].legend(fontsize=9)
    absolute = np.sort(np.abs(normalized)/normalized.std(ddof=1))
    positive_indices = np.flatnonzero(absolute>0)
    selected = positive_indices[np.unique(np.linspace(0,len(positive_indices)-1,4000).astype(int))]
    axes[1,0].loglog(absolute[selected], (len(absolute)-selected)/len(absolute), color=blue, label='Observed (includes zero-return mass)')
    x = np.geomspace(.05, max(6, absolute[-1]), 1000)
    axes[1,0].loglog(x, 2*stats.norm.sf(x), color=grey, linestyle='--', label='Gaussian; same unit std')
    axes[1,0].set(xlim=(.05, absolute[-1]*1.05), ylim=(1/len(absolute),1),title='G. Absolute-return survival; not a power-law fit', xlabel='|Normalized return| / std (log scale)', ylabel='Fraction >= x (log scale)')
    axes[1,0].legend(fontsize=9)
    observed = diagnostics['memory']['normalized']['20']
    sizes = np.array(observed['sizes'])
    reference = np.array([r['20']['statistic'] for r in diagnostics['block_shuffle_references']])
    axes[1,1].fill_between(sizes, reference.min(axis=0), reference.max(axis=0),color=grey,alpha=.25,label='10 block shuffles (range, not CI)')
    axes[1,1].loglog(sizes, observed['statistic'], 'o-',color=orange,label=f"Observed; large-block slope {observed['large_block_scaling_slope']:.3f}")
    axes[1,1].set(title='H. Modified R/S on |Normalized returns|; q=20',xlabel='Block length (ticks; log scale)',ylabel='Median modified R/S (log scale)')
    axes[1,1].legend(fontsize=9)
    fig.savefig(args.output/'diagnostics.png', dpi=160)
    fig.savefig(args.output/'diagnostics.svg'); plt.close(fig)
    summary = dict(source=str(args.source), source_sha256=hashlib.sha256((args.source/'series.npz').read_bytes()).hexdigest(),
        config=cfg, selection=f"Declared run, seed {cfg['seed']}, cap {cfg['position_cap']}; zoom is the first 4000 ticks after burn-in, not a selected extreme.",
        zoom=dict(start=start, stop=stop, min_price=float(prices[start:stop+1].min()), max_price=float(prices[start:stop+1].max()),
                  price_range_pct=float((prices[start:stop+1].max()/prices[start:stop+1].min()-1)*100),
                  block_std_50_min_pct=float(block_std.min()*100), block_std_50_max_pct=float(block_std.max()*100)),
        normalized_spectral_peak_period_ticks=period, normalized_spectral_three_bin_share=share,
        metrics=result['metrics'], simulation=result['simulation'])
    (args.output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps({k:v for k,v in summary.items() if k not in ('simulation','config')},indent=2))


if __name__ == '__main__':
    main()
