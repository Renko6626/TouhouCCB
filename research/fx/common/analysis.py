"""Model-independent diagnostics on explicitly aligned return samples."""
import numpy as np
from .diagnostics import acf, describe
from .scaling import DFA_SIZES, dfa, fits
from .series import block_mean, correlation

SIZES = DFA_SIZES
LAG_BANDS = [(1,1),(2,10),(11,30),(31,100),(101,300),(301,1000),(1001,5000)]


def state_diagnostics(state, returns, block_widths=(30,100,240,1000)):
    """State[t] precedes return[t]; compare same and next non-overlapping blocks.

    Constant states retain an undefined ACF and None correlations. The caller
    owns burn-in and timing; this function never shifts data to fit a model.
    """
    values=np.asarray(state,dtype=float)
    r=np.asarray(returns,dtype=float)
    if values.ndim!=1 or values.shape!=r.shape:
        raise ValueError('state and returns must be aligned one-dimensional samples')
    curve=acf(values,5000)
    blocks=[]
    for width in block_widths:
        predicted,actual=block_mean(values,width),block_mean(r*r,width)
        blocks.append(dict(width=width,complete_blocks=len(predicted),next_pairs=max(0,len(predicted)-1),
                           same_block_corr=correlation(predicted,actual),
                           next_block_corr=correlation(predicted[:-1],actual[1:])))
    return dict(acf=curve.tolist(),blocks=blocks,
                acf_bands={f'{lo}-{hi}':float(curve[lo:hi+1].mean())
                           if len(curve)>hi and np.isfinite(curve[lo:hi+1]).all() else None
                           for lo,hi in LAG_BANDS})


def return_diagnostics(returns):
    r = np.asarray(returns, dtype=float)
    absolute = acf(abs(r), 5000)
    return dict(returns=describe(r), nonzero_returns=describe(r[r != 0]),
        absolute_acf=absolute.tolist(), signed_acf=acf(r, 5000).tolist(), squared_acf=acf(r*r, 5000).tolist(),
        abs_acf_bands={f'{lo}-{hi}':float(np.mean(absolute[lo:hi+1])) for lo, hi in
                       [(1,1),(2,10),(11,30),(31,100),(101,300),(301,1000),(1001,5000)]},
        aggregation={str(w): describe(r[:len(r)//w*w].reshape(-1, w).sum(axis=1)) for w in [1,5,30,60,240,1000]},
        dfa={name: dict(curve=dfa(values, SIZES), fits=fits(dfa(values, SIZES)),
                       dfa2_fits=fits(dfa(values, SIZES, order=2))) for name, values in
             [('returns', r), ('absolute_returns', abs(r))]})
