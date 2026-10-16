"""DFA finite-scale curves and fixed-range fits; no experiment CLI."""
import numpy as np

DFA_SIZES = [16, 24, 32, 48, 64, 96, 128, 192, 256, 384, 512, 768, 1024, 1536, 2048, 3072, 4096, 6144, 8192]


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
