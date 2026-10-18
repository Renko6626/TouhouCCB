"""Summarize fixed versus mixed windows using saved experimental evidence."""
import argparse
import csv
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=Path('research/fx/results/position-window-mix'))
    root=parser.parse_args().source
    output=root/'overview';output.mkdir(exist_ok=True)
    rows=[json.loads(p.read_text()) for p in root.glob('*/result.json')]
    manifest=json.loads((root/'manifest.json').read_text())
    assert len(rows)==len(manifest['configs']) and all(r['status']=='complete' for r in rows)
    factor_mode=any(r['metadata']['config'].get('factor_mix') for r in rows)
    groups={}
    detail=[]
    for r in rows:
        c=r['metadata']['config'];s=r['simulation'];m=r['metrics']
        if factor_mode:
            name='Mixed trend/reversion' if c.get('factor_mix') else f"a={c['trend_strength']:g}, b={c['reversion_strength']:g}"
        else:
            name='Mixed 10/30/100/300' if c['window_mix'] else f"All {c['trend_window']}"
        groups.setdefault(name,[]).append(r)
        detail.append(dict(group=name,seed=c['seed'],id=r['id'],range_pct=s['range_pct'],gold_volume=s['actual_gold_volume'],
            **m,**{k+'_std':v['std'] for k,v in s['factors']['all_updates'].items()}))
    with (output/'comparison.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(detail[0]));writer.writeheader();writer.writerows(detail)
    metrics=['range_pct','return_acf1','normalized_abs_acf1','normalized_abs_acf100_1000','normalized_rs_q20','normalized_kurtosis','gold_volume','trend_std','reversion_std','noise_std']
    summaries={name:{key:dict(mean=float(np.mean([d[key] for d in detail if d['group']==name])),
                            minimum=float(min(d[key] for d in detail if d['group']==name)),
                            maximum=float(max(d[key] for d in detail if d['group']==name))) for key in metrics} for name in groups}
    (output/'summary.json').write_text(json.dumps(summaries,indent=2)+'\n')
    names=['a=30, b=0.1','a=3, b=1','a=16.5, b=0.55','Mixed trend/reversion'] if factor_mode else ['All 10','All 30','All 100','All 300','Mixed 10/30/100/300']
    fig,axes=plt.subplots(len(names),2,figsize=(12,13),constrained_layout=True)
    for i,name in enumerate(names):
        r=next(r for r in groups[name] if r['metadata']['config']['seed']==min(row['metadata']['config']['seed'] for row in groups[name]))
        p=root/r['id'];z=np.load(p/'series.npz');d=json.loads((p/'diagnostics.json').read_text());start=5000;stop=9000
        axes[i,0].plot(np.arange(start,stop+1),z['prices'][start:stop+1],color='#2563eb',linewidth=.8)
        axes[i,0].set(title=f"{name}; seed {r['metadata']['config']['seed']} (independent price scales)",xlabel='Tick',ylabel='Gold / foreign')
        for key,color,label,style in [('returns','#2563eb','Returns','-'),('normalized_absolute','#d97706','|Normalized returns|','--')]:
            values=d['correlations'][key][1:1001]
            axes[i,1].plot(np.arange(1,len(values)+1),values,color=color,label=label,linestyle=style,linewidth=.8)
        axes[i,1].set(xlabel='Lag (ticks)',ylabel='ACF');axes[i,1].axhline(0,color='#64748b',linewidth=.5);axes[i,1].legend(fontsize=8)
    fig.savefig(output/('factors.png' if factor_mode else 'windows.png'),dpi=150);plt.close(fig)
    print(json.dumps(summaries,indent=2))


if __name__=='__main__':main()
