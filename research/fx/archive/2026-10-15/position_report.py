"""Plot the approved bounded-position comparison, preserving invalid prefixes."""

import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np


def main():
    root=Path('research/fx/results/position-comparison')
    rows=[json.loads(p.read_text()) for p in root.glob('*/result.json')]
    summaries=[]
    for row in rows:
        c=row['metadata']['config'];s=row['simulation']
        summaries.append(dict(strategy=c['strategy'],cap=c['position_cap'] if c['strategy']=='position' else '',
            trend=c['trend_strength'],reversion=c['reversion_strength'],noise=c['noise_sigma'],seed=c['seed'],
            status=row['status'],trades=s['trades'],arrivals=s['arrivals'],min_price=s['min_price'],max_price=s['max_price'],
            range_pct=s['range_pct'],gold_turnover=s['actual_gold_volume'],foreign_turnover=s['gross_foreign_turnover'],
            max_abs_position=s['max_abs_foreign_position'],fees_foreign=s['fees_foreign']))
    with (root/'comparison.csv').open('w',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=list(summaries[0]))
        writer.writeheader();writer.writerows(summaries)
    fig,axes=plt.subplots(2,2,figsize=(12,8),constrained_layout=True)
    for i,trend in enumerate((30,100)):
        for strategy,cap,color in [('position',1000,'#d97706'),('position',500,'#2563eb'),('fixed',500,'#64748b')]:
            row=next(r for r in rows if r['metadata']['config']['seed']==1 and r['metadata']['config']['trend_strength']==trend
                     and r['metadata']['config']['strategy']==strategy and r['metadata']['config']['position_cap']==cap)
            prices=np.load(root/row['id']/'series.npz')['prices']
            rejection=row['simulation']['first_rejection']
            length=rejection['tick'] if rejection else len(prices)
            label='Fixed 200' if strategy=='fixed' else f'Target cap {cap}'
            index=np.unique(np.linspace(0,length-1,min(length,4000)).astype(int))
            axes[i,0].plot(index,prices[index],color=color,label=label)
            zoom=min(2001,length)
            axes[i,1].plot(np.arange(zoom),prices[:zoom],color=color,label=label)
            if rejection:
                axes[i,0].plot(index[-1],prices[index[-1]],'x',color='#dc2626',label='First rejected order; curve stops')
        for j in range(2):
            axes[i,j].set(title=f'a={trend}; seed 1'+(' (first 2000 ticks)' if j else ''),xlabel='Tick',ylabel='Gold / foreign')
            axes[i,j].legend(fontsize=8);axes[i,j].spines[['top','right']].set_visible(False)
    fig.savefig(root/'prices.png',dpi=150);plt.close(fig)
    print('saved comparison.csv and prices.png')


if __name__=='__main__':
    main()
