"""Quenched personal coefficients with controlled class-level first/second moments."""
import numpy as np


def make_profiles(cfg,population_seed,spread):
    if spread<0:raise ValueError('spread must be nonnegative')
    n=cfg['accounts'];groups=np.arange(n)%len(cfg['factor_mix'])
    rng=np.random.default_rng(np.random.SeedSequence([population_seed,20261015,173]))
    multipliers=np.exp(spread*rng.normal(size=(n,3)))
    for group in range(len(cfg['factor_mix'])):
        mask=groups==group
        multipliers[mask,:2]/=multipliers[mask,:2].mean(axis=0)
        multipliers[mask,2]/=np.sqrt(np.mean(multipliers[mask,2]**2))
    return [dict(account=i,type=int(groups[i]),trend=float(cfg['factor_mix'][groups[i]][0]*multipliers[i,0]),
                 reversion=float(cfg['factor_mix'][groups[i]][1]*multipliers[i,1]),
                 sigma=float(cfg['noise_sigma']*multipliers[i,2])) for i in range(n)]
