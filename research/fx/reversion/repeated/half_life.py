"""Observed half-retracement times on the saved path, with right censoring."""
import csv
import hashlib
import json

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

from .run import OUTPUT


def main():
    inputs = [OUTPUT/'shock.npz', OUTPUT/'summary.json']
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
    prices = np.load(inputs[0])['prices']
    events = json.loads(inputs[1].read_text())['shock']['events']
    rows, full_curves = [], []
    for index, event in enumerate(events):
        start = event['tick']
        # Exclude the next event tick; never use a later shock as a recovery.
        end = events[index+1]['tick']-1 if index+1 < len(events) else len(prices)-1
        pre, post = float(event['pre_price']), float(event['post_price'])
        remaining = (prices[start:end+1]-pre)/(post-pre)
        assert np.isclose(remaining[0], 1)
        crossings = np.flatnonzero(remaining <= .5)
        sustained = [t for t in range(1, len(remaining)-11) if np.all(remaining[t:t+12] <= .5)]
        smooth = np.convolve(remaining, np.ones(12)/12, mode='valid') if len(remaining) >= 12 else np.array([])
        smooth_crossings = np.flatnonzero(smooth <= .5)
        row = dict(tick=start, direction='up' if post > pre else 'down', observed_minutes=(end-start)/6,
            first_half_minutes=float(crossings[0]/6) if len(crossings) else None,
            sustained_12_closes_start_minutes=float(sustained[0]/6) if sustained else None,
            trailing_12_close_mean_half_minutes=float((smooth_crossings[0]+11)/6) if len(smooth_crossings) else None,
            max_observed_retracement=float(1-remaining.min()), last_observed_retracement=float(1-remaining[-1]))
        if len(crossings):
            t = int(crossings[0])
            assert np.all(remaining[:t] > .5)
            # Independent price-level inequality, valid for both jump signs.
            assert (prices[start+t]-(pre+post)/2)*np.sign(post-pre) <= 1e-12
        rows.append(row)
        if end-start == 999:
            full_curves.append(remaining)
    curves = np.array(full_curves)
    mean = curves.mean(axis=0)
    peak = int(np.argmax(1-mean))
    aggregate = dict(observed_events=len(curves), first_half_count=sum(r['first_half_minutes'] is not None for r in rows),
        sustained_half_count=sum(r['sustained_12_closes_start_minutes'] is not None for r in rows),
        mean_curve_reaches_half=bool(np.any(mean <= .5)),
        peak_mean_retracement=float(1-mean[peak]), peak_mean_minutes=peak/6,
        sample_mean_retracement={str(m):float(1-mean[int(m*6)]) for m in [5,10,15,20,30,60,120,166.5]})
    result = dict(events=rows, aggregate=aggregate, sources_sha256=hashes,
        definition='Remaining=(P_after_n_ticks-P_immediately_before_event)/(P_immediately_after_event-P_immediately_before_event). Half is remaining<=0.5. Subsequent event tick excluded; terminal event has zero follow-up. Sustained means 12 consecutive closes (~2 min), labeled by first close; known only after 11 further ticks. No permanent-impact adjustment or exponential fit.')
    (OUTPUT/'half_life.json').write_text(json.dumps(result, indent=2, allow_nan=False)+'\n')
    with (OUTPUT/'half_life.csv').open('w') as f:
        writer=csv.DictWriter(f, fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    plt.rcParams.update({'font.family':'Noto Sans CJK SC', 'axes.unicode_minus':False})
    fig, ax = plt.subplots(figsize=(10,5))
    minutes=np.arange(curves.shape[1])/6
    for i, curve in enumerate(curves):
        ax.plot(minutes,(1-curve)*100, color='#7694ae', alpha=.3, lw=.7,
                label='9次事件各自路径' if i==0 else None)
    ax.plot(minutes,(1-mean)*100,color='#123e66',lw=2,label='按各自跳幅归一化后等权平均')
    ax.axhline(50,color='#ad6036',ls='--',label='回吐一半')
    ax.axhline(0,color='gray',lw=.6)
    ax.set(xlabel='冲击后分钟（下次冲击前截断）',ylabel='原始冲击回吐比例（%）',xlim=(0,166.5),
           title='半幅首次触及不等于持续衰减；平均路径未达50%')
    ax.grid(alpha=.2);ax.legend(fontsize=9);fig.tight_layout();fig.savefig(OUTPUT/'half_life.png',dpi=150);plt.close(fig)
    for p in inputs:
        assert hashlib.sha256(p.read_bytes()).hexdigest() == hashes[p.name]
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
