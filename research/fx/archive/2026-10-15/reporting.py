"""Inspectible tables and scientific plots from completed experiment files."""

import argparse
import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np


def load_rows(directory):
    rows=[]
    for path in sorted(Path(directory).glob('*/result.json')):
        row=json.loads(path.read_text())
        if row['status']=='complete':
            diagnostic=json.loads((path.parent/'diagnostics.json').read_text())
            row['metrics']['normalized_std']=diagnostic['normalized_distribution']['std']
        rows.append(row)
    return rows


def export_rows(rows,path):
    records=[]
    for row in rows:
        cfg=row['metadata']['config'];sim=row.get('simulation',{})
        record=dict(id=row['id'],status=row['status'],**cfg,**row.get('metrics',{}))
        for key in ('trades','final_price','range_pct','actual_gold_volume','net_pool_gold_flow','rejected_orders'):
            record[key]=sim.get(key)
        record['first_rejection']=json.dumps(sim.get('first_rejection'))
        records.append(record)
    fields=list(dict.fromkeys(k for r in records for k in r))
    with Path(path).open('w',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=fields)
        writer.writeheader();writer.writerows(records)


def grid_figures(rows,output):
    for metric,title in [('normalized_abs_acf100_1000','Normalized |return| ACF: mean lags 100-1000'),
                         ('normalized_kurtosis','Pool-depth normalized excess kurtosis')]:
        values=[r['metrics'][metric] for r in rows if r['status']=='complete']
        lower,upper=min(values),max(values)
        fig,axes=plt.subplots(3,3,figsize=(12,10),constrained_layout=True)
        image=None
        for i,noise in enumerate((.001,.005,.02)):
            for j,window in enumerate((30,100,300)):
                axis=axes[i,j]; matrix=np.full((3,3),np.nan)
                for row in rows:
                    c=row['metadata']['config']
                    if c['noise_sigma']==noise and c['trend_window']==window and row['status']=='complete':
                        a=(0,30,100).index(c['trend_strength']);b=(0,.1,1).index(c['reversion_strength'])
                        matrix[a,b]=row['metrics'][metric]
                image=axis.imshow(matrix,vmin=lower,vmax=upper,cmap='viridis',aspect='auto')
                for a in range(3):
                    for b in range(3):
                        value=matrix[a,b]
                        label=f'{value:.4f}' if metric.startswith('normalized_abs') else f'{value:.2f}'
                        axis.text(b,a,label if np.isfinite(value) else 'invalid',ha='center',va='center',fontsize=9,
                                  color='white' if np.isfinite(value) and (value-lower)/(upper-lower or 1)<.6 else 'black')
                axis.set(title=f'noise={noise:g}, L={window}',xlabel='Reversion b',ylabel='Trend a')
                axis.set_xticks(range(3),labels=['0','0.1','1']);axis.set_yticks(range(3),labels=['0','30','100'])
        fig.colorbar(image,ax=axes.ravel().tolist(),shrink=.7)
        fig.suptitle(title+'; seed 1 screening, not a significance test',fontsize=13)
        fig.savefig(output/f'grid-{metric}.png',dpi=150);plt.close(fig)


def flow_figures(rows,output):
    fig,axes=plt.subplots(1,2,figsize=(11,4),constrained_layout=True)
    for metric,axis in [('normalized_std',axes[0]),('normalized_kurtosis',axes[1])]:
        group=[]
        for bots,size in [(5,800),(20,200),(80,50)]:
            selected=[r['metrics'][metric] for r in rows if r['status']=='complete' and
                      r['metadata']['config']['bots']==bots and r['metadata']['config']['order_size']==size
                      and r['metadata']['config']['rate']==.05]
            group.append(selected)
        multiplier=100 if metric=='normalized_std' else 1
        for i,values in enumerate(group):
            axis.scatter([i]*len(values),np.array(values)*multiplier,color='#2563eb')
        axis.plot(range(3),[np.mean(x)*multiplier for x in group],color='#2563eb',label='Three-seed mean')
        expected=[.08,.04,.02] if metric=='normalized_std' else [4,1,.25]
        axis.plot(range(3),expected,'--',color='#475569',label='Independent small-order approximation')
        axis.set_xticks(range(3),labels=['5 bots x 800','20 bots x 200','80 bots x 50'])
        axis.set(ylabel='Normalized 1-tick return std (%)' if metric=='normalized_std' else 'Normalized excess kurtosis',
                 title='Equal expected gross flow: 200 gold/tick')
        axis.legend();axis.spines[['top','right']].set_visible(False)
    fig.savefig(output/'flow-controls.png',dpi=150);plt.close(fig)


def million_figure(directory,rows,output):
    fig,axes=plt.subplots(3,1,figsize=(11,9),constrained_layout=True)
    for axis,seed in zip(axes,(5,6,7)):
        for window,color in [(30,'#64748b'),(1000,'#2563eb')]:
            row=next(r for r in rows if r['metadata']['config']['seed']==seed and
                     r['metadata']['config']['trend_window']==window)
            with np.load(directory/row['id']/'series.npz') as data:
                prices=data['prices']
            rejection=row['simulation'].get('first_rejection')
            length=rejection['tick'] if rejection else len(prices)
            index=np.unique(np.linspace(0,length-1,min(length,4000)).astype(int))
            axis.plot(index,prices[index],color=color,label=f'L={window}')
            if rejection:
                axis.plot(index[-1],prices[index[-1]],'x',color='#dc2626',markersize=9,
                          label=f'L={window}: balance assumption fails at {rejection["tick"]:,}')
        axis.set(title=f'Seed {seed}: valid prefix only when balances bind',xlabel='Tick',ylabel='Gold / foreign')
        axis.legend(fontsize=9);axis.spines[['top','right']].set_visible(False)
    fig.savefig(output/'million-evolution.png',dpi=150);plt.close(fig)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path('research/fx/results'))
    args=parser.parse_args()
    output=args.root/'overview';output.mkdir(exist_ok=True)
    for group in ['factor-grid','flow-grid','flow-controls','near-critical','confirmation','critical-confirmation','million']:
        rows=load_rows(args.root/group)
        if rows:
            export_rows(rows,output/f'{group}.csv')
    grid_figures(load_rows(args.root/'factor-grid'),output)
    flow_figures(load_rows(args.root/'flow-controls'),output)
    million_rows=load_rows(args.root/'million')
    if len(million_rows)==6 and all(r['status'] in ('complete','invalid_model_regime') for r in million_rows):
        million_figure(args.root/'million',million_rows,output)
    print(f'saved {output}')


if __name__=='__main__':
    main()
