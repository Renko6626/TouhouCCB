"""Descriptive diagnostics and explicitly scoped hypothesis checks for AMM runs."""

import numpy as np

from .diagnostics import (acf, block_shuffle, describe, pareto_bootstrap,
                          pareto_fit, rs_curve, tail_alternatives)


def tail_diagnostics(values, bootstrap, seed, minimum=500):
    positive = np.abs(values)
    positive = positive[np.isfinite(positive) & (positive > 0)]
    if len(positive) < minimum*5:
        return {'status':'insufficient_tail_sample', 'positive_n':len(positive)}
    fit = pareto_fit(positive, minimum)
    fit['status'] = 'descriptive_only'
    fit['alternatives'] = tail_alternatives(positive, fit)
    fit['hill'] = {}
    for quantile in (.8,.9,.95,.975,.99,.995):
        threshold = np.quantile(positive,quantile)
        tail = positive[positive >= threshold]
        denominator = np.log(tail/threshold).sum()
        fit['hill'][str(quantile)] = dict(beta=float(len(tail)/denominator) if denominator > 0 else None,
                                        n=len(tail), xmin=float(threshold))
    fit['exact_unique_fraction'] = float(len(np.unique(positive))/len(positive))
    fit['null'] = 'continuous iid Pareto tail above a threshold selected on a fixed quantile grid'
    if bootstrap:
        fit['gof'] = pareto_bootstrap(positive,fit,bootstrap,seed,minimum)
        fit['status'] = 'reject_continuous_iid_null' if fit['gof']['p_iid'] < .1 else 'not_rejected_iid_only'
    fit['interpretation'] = ('Neither status establishes or excludes heavy tails, long memory, or market realism; '
                             'order discreteness, pool-depth changes and dependence affect this null.')
    return fit


def analyze(prices, gold, *, burn_in=2000, bootstrap=0, seed=1, shuffle_repetitions=1):
    returns = np.diff(np.log(prices))[burn_in:]
    normalized = returns*gold[:-1][burn_in:]/gold[0]
    if len(returns)<20000 or not np.isfinite(normalized).all():
        raise ValueError('need >=20000 finite post-burn-in returns')
    lag = min(5000,len(returns)//10)
    correlations = {name:acf(values,lag).tolist() for name,values in
                    [('returns',returns),('absolute',abs(returns)),('squared',returns**2),
                     ('normalized_absolute',abs(normalized))]}
    prefix = len(returns)//60*60
    memory = {name:{str(q):rs_curve(values,q) for q in (0,20,100)}
              for name,values in [('raw',abs(returns[:prefix])),('normalized',abs(normalized[:prefix]))]}
    # Conditional permutation references, not formal CIs/p-values for long memory.
    references = []
    for repetition in range(shuffle_repetitions):
        shuffled=block_shuffle(abs(normalized[:prefix]),60,seed+repetition*1009)
        references.append({str(q):rs_curve(shuffled,q) for q in (20,100)})
    aggregation={}
    for width in (1,5,30,60,240,1000):
        aggregated=returns[:len(returns)//width*width].reshape(-1,width).sum(axis=1)
        aggregation[str(width)]=describe(aggregated)
    return dict(schema=2, n=len(returns), memory_sample_n=prefix, distribution=describe(returns),
                normalized_distribution=describe(normalized),
                positive_tail=tail_diagnostics(returns[returns>0],bootstrap,seed),
                negative_tail=tail_diagnostics(returns[returns<0],bootstrap,seed+100),
                absolute_tail=tail_diagnostics(returns,bootstrap,seed+200),
                normalized_tail=tail_diagnostics(normalized,bootstrap,seed+300),
                thinned_tail=tail_diagnostics(returns[::60],bootstrap,seed+400,minimum=100),
                correlations=correlations,memory=memory,block_shuffle_references=references,
                memory_interpretation='Finite-scale diagnostics only; q100 small-block slopes are biased; '
                                     'conditional shuffle bands are not long-memory tests.',
                aggregation=aggregation,time_blocks=[describe(x) for x in np.array_split(returns,10)])


def compact(result):
    """No pass/fail score: preserve separate descriptive axes for screening."""
    tail=result['absolute_tail']
    reference=result['block_shuffle_references'][0]['20']
    reference_slopes=[r['20']['large_block_scaling_slope'] for r in result['block_shuffle_references']]
    reference_slopes=[s for s in reference_slopes if s is not None]
    return dict(excess_kurtosis=result['distribution']['excess_kurtosis'],
                normalized_kurtosis=result['normalized_distribution']['excess_kurtosis'],
                std=result['distribution']['std'],zero_fraction=result['distribution']['zero_fraction'],
                return_acf1=result['correlations']['returns'][1],
                abs_acf1=result['correlations']['absolute'][1],
                normalized_abs_acf1=result['correlations']['normalized_absolute'][1],
                normalized_abs_acf100_1000=float(np.mean(result['correlations']['normalized_absolute'][100:1001])),
                normalized_rs_q20=result['memory']['normalized']['20']['large_block_scaling_slope'],
                shuffled_rs_q20=reference['large_block_scaling_slope'],
                shuffled_rs_q20_mean=float(np.mean(reference_slopes)) if reference_slopes else None,
                shuffled_rs_q20_reference_range=[float(min(reference_slopes)),float(max(reference_slopes))] if reference_slopes else None,
                tail_beta=tail.get('beta'),tail_decades=tail.get('tail_decades'),
                tail_status=tail['status'],tail_p_iid=tail.get('gof',{}).get('p_iid'))
