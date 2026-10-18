"""DFA finite-scale diagnostics on saved returns, with explicit scale ranges."""
import argparse
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np


def dfa(values, sizes, order=1):
    x=np.asarray(values,dtype=float)
    if x.ndim!=1 or len(x)<2 or not np.isfinite(x).all():
        raise ValueError('finite one-dimensional data required')
    profile=np.cumsum(x-x.mean())
    result=[]
    for size in sizes:
        count=len(x)//size
        if count<8:continue
        # Forward and backward segmentation includes both endpoints; overlapping
        # segments do not count as independent samples or confidence intervals.
        blocks=np.concatenate((profile[:count*size].reshape(count,size),profile[-count*size:].reshape(count,size)))
        coordinate=np.linspace(-1,1,size)
        basis=np.polynomial.polynomial.polyvander(coordinate,order)
        q,_=np.linalg.qr(basis)
        residual=blocks-(blocks@q)@q.T
        result.append(dict(size=int(size),segments_per_direction=int(count),F=float(np.sqrt(np.mean(residual**2)))))
    return result


def fits(curve):
    result={}
    for lo,hi in [(32,256),(512,8192),(2048,8192)]:
        points=[p for p in curve if lo<=p['size']<=hi and p['F']>0]
        if len(points)<3:continue
        x=np.log([p['size'] for p in points]);y=np.log([p['F'] for p in points]);slope,intercept=np.polyfit(x,y,1)
        residual=y-slope*x-intercept
        result[f'{lo}-{hi}']=dict(alpha=float(slope),r_squared=float(1-np.sum(residual**2)/np.sum((y-y.mean())**2)),points=len(points))
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=Path('research/fx/results/position-factor-mix-cap500'))
    parser.add_argument('--output',type=Path,default=Path('research/fx/results/position-factor-mix-cap500/hurst'))
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    sizes=[16,24,32,48,64,96,128,192,256,384,512,768,1024,1536,2048,3072,4096,6144,8192]
    results=[]
    for path in sorted(args.source.glob('*/result.json')):
        original=json.loads(path.read_text());assert original['status']=='complete'
        cfg=original['metadata']['config'];data=np.load(path.parent/'series.npz')
        returns=np.diff(np.log(data['prices']))[cfg['burn_in']:]
        normalized=returns*data['gold'][:-1][cfg['burn_in']:]/data['gold'][0]
        values={'returns':returns,'absolute_returns':abs(returns),'normalized_absolute_returns':abs(normalized),'squared_returns':returns**2}
        result=dict(id=original['id'],seed=cfg['seed'],config=cfg,n=len(returns),statistics={})
        for key,x in values.items():
            curve=dfa(x,sizes);curve2=dfa(x,sizes,order=2)
            shuffles=[]
            for repeat in range(10):
                shuffled=np.random.default_rng(cfg['seed']+1009*repeat).permutation(x)
                shuffles.append(fits(dfa(shuffled,sizes)))
            result['statistics'][key]=dict(dfa1=curve,fits=fits(curve),dfa2_fits=fits(curve2),iid_shuffle_fits=shuffles)
        # Exact identity check for the cumulative return profile.
        price_changes=np.log(data['prices'][cfg['burn_in']+1:])-np.log(data['prices'][cfg['burn_in']])
        assert np.allclose(np.cumsum(returns),price_changes,rtol=1e-10,atol=1e-12)
        results.append(result)
    (args.output/'summary.json').write_text(json.dumps(dict(method='DFA1 and DFA2; finite-scale slopes, not certified Hurst parameters',sizes=sizes,results=results),indent=2)+'\n')
    cases=sorted({(tuple(r['config'].get('window_mix',())),r['config']['adjustment']) for r in results})
    fig,axes=plt.subplots(len(cases),2,figsize=(12,5*len(cases)),squeeze=False,constrained_layout=True)
    for i,case in enumerate(cases):
        selected=sorted([r for r in results if (tuple(r['config'].get('window_mix',())),r['config']['adjustment'])==case],key=lambda r:r['seed'])
        for result,color in zip(selected,['#2563eb','#d97706','#64748b']):
            for ax,key in zip(axes[i],['returns','absolute_returns']):
                curve=result['statistics'][key]['dfa1'];ax.loglog([p['size'] for p in curve],[p['F'] for p in curve],'o-',color=color,label=f"seed {result['seed']}",markersize=3)
        window_label='/'.join(map(str,sorted(set(case[0])))) if case[0] else str(selected[0]['config']['trend_window'])
        for ax,title in zip(axes[i],['Signed returns','Volatility proxy |returns|']):
            ax.set(title=f'{title}\nWindows {window_label}; adjustment {case[1]}',xlabel='DFA scale (ticks)',ylabel='RMS detrended cumulative fluctuation');ax.legend();ax.axvline(512,color='#cbd5e1',linestyle='--')
    fig.savefig(args.output/'dfa.png',dpi=160);plt.close(fig)
    for result in results:
        print('seed',result['seed'])
        for key,stats in result['statistics'].items():
            print(key,{k:round(v['alpha'],4) for k,v in stats['fits'].items()},'DFA2',{k:round(v['alpha'],4) for k,v in stats['dfa2_fits'].items()})


if __name__=='__main__':main()
