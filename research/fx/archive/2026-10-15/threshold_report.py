"""Inspect the approved threshold comparison; no automatic realism verdict."""
import csv
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from .hurst import dfa, fits
from .diagnostics import acf

ROOT=Path('research/fx/results/threshold-comparison')
OUT=ROOT/'overview'
SIZES=[16,24,32,48,64,96,128,192,256,384,512,768,1024,1536,2048,3072,4096,6144,8192]


def label(cfg):
    if cfg['strategy']=='position':return 'continuous'
    kind='mixed' if cfg['threshold_mix'] else 'uniform'
    return f"{kind} T={cfg['entry_threshold']:g}"


def main():
    OUT.mkdir(exist_ok=True)
    runs=[]
    from .experiments import expand
    included={c.identifier() for c in expand(json.loads(Path("research/fx/configs/threshold-comparison.json").read_text()))}
    for path in sorted(ROOT.glob('*/result.json')):
        r=json.loads(path.read_text())
        if r['id'] not in included:continue
        assert r['status']=='complete',path
        cfg=r['metadata']['config'];data=np.load(path.parent/'series.npz')
        x=np.diff(np.log(data['prices']))[cfg['burn_in']:]
        row=dict(case=label(cfg),seed=cfg['seed'],id=r['id'],range_pct=r['simulation']['range_pct'],
                 gold_volume=r['simulation']['actual_gold_volume'],**r['metrics'])
        row.update(r['simulation'].get('threshold_activity',{}));row.pop('thresholds',None)
        stats={}
        for name,values in [('returns',x),('absolute_returns',abs(x))]:
            curve=dfa(values,SIZES); fit=fits(curve); fit2=fits(dfa(values,SIZES,order=2))
            reference=[fits(dfa(np.random.default_rng(cfg['seed']+1009*j).permutation(values),SIZES)) for j in range(3)]
            stats[name]=dict(curve=curve,fits=fit,dfa2_fits=fit2,shuffle_fits=reference)
            for scale in ('32-256','512-8192','2048-8192'):
                row[f'{name}_dfa1_{scale}']=fit[scale]['alpha']
                row[f'{name}_dfa2_{scale}']=fit2[scale]['alpha']
        time_blocks=[]
        raw=np.diff(np.log(data['prices']))
        for start in range(0,len(raw),10000):
            block=raw[start:start+10000]
            correlations=acf(abs(block),min(1000,len(block)-1))
            correlation=float(np.mean(correlations[100:1001]))
            time_blocks.append(dict(start=start,n=len(block),std=float(block.std()),zero_fraction=float(np.mean(block==0)),
                                    abs_acf100_1000=correlation if np.isfinite(correlation) else None))
        runs.append(dict(row=row,dfa=stats,time_blocks=time_blocks))
    (OUT/'statistics.json').write_text(json.dumps(runs,indent=2)+'\n')
    keys=list(dict.fromkeys(k for r in runs for k in r['row']))
    with (OUT/'runs.csv').open('w') as f:
        writer=csv.DictWriter(f,keys);writer.writeheader();writer.writerows(r['row'] for r in runs)
    names=sorted({r['row']['case'] for r in runs})
    means=[]
    for name in names:
        selected=[r['row'] for r in runs if r['row']['case']==name]
        values={k:float(np.mean([r[k] for r in selected])) for k in keys if all(isinstance(r.get(k),(int,float)) for r in selected) and k!='seed'}
        means.append(dict(case=name,**values))
    (OUT/'means.json').write_text(json.dumps(means,indent=2)+'\n')
    fig,axes=plt.subplots(len(names),2,figsize=(13,2.3*len(names)),constrained_layout=True)
    for axes_row,name in zip(axes,names):
        r=next(r for r in runs if r['row']['case']==name and r['row']['seed']==41)
        data=np.load(ROOT/r['row']['id']/'series.npz');price=data['prices'];returns=np.diff(np.log(price))
        width=100;std=returns[:len(returns)//width*width].reshape(-1,width).std(axis=1)
        axes_row[0].plot(np.arange(len(price))[::20],price[::20],lw=.5,color='#2563eb')
        axes_row[1].plot((np.arange(len(std))+.5)*width,std,lw=.5,color='#64748b')
        axes_row[0].set(title=name,ylabel='Price',xlabel='Tick')
        axes_row[1].set(title='Non-overlapping 100-tick return std',xlabel='Tick',ylabel='Std')
    fig.savefig(OUT/'paths.png',dpi=150);plt.close(fig)
    fig,axes=plt.subplots(1,2,figsize=(13,5),constrained_layout=True)
    for ax,key in zip(axes,['returns','absolute_returns']):
        for name in names:
            selected=[r for r in runs if r['row']['case']==name]
            curves=[r['dfa'][key]['curve'] for r in selected]
            colors={'0.0005':'#2563eb','0.001':'#b7791f','0.002':'#708238'}
            color=colors.get(name.split('=')[-1],'#475569')
            ax.loglog([v['size'] for v in curves[0]],np.exp(np.mean([[np.log(v['F']) for v in c] for c in curves],axis=0)),label=name,color=color,linestyle='--' if name.startswith('uniform') else '-')
        ax.set(title=key,xlabel='DFA scale (ticks)',ylabel='Geometric mean F over 3 seeds');ax.legend(fontsize=8)
    fig.savefig(OUT/'dfa.png',dpi=150);plt.close(fig)
    for r in means:
        print(r['case'],{k:round(r[k],4) for k in ['range_pct','gold_volume','return_acf1','abs_acf1','normalized_abs_acf100_1000','excess_kurtosis','returns_dfa1_512-8192','absolute_returns_dfa1_512-8192']})


if __name__=='__main__':main()
