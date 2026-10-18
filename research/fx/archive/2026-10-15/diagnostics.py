"""Scientific diagnostics. NumPy/SciPy are analysis-only dependencies."""

import numpy as np
from scipy import optimize, special, stats


def acf(values, max_lag=5000):
    values = np.asarray(values, dtype=float)
    if values.ndim != 1 or len(values) < 2 or not np.isfinite(values).all():
        raise ValueError('ACF needs a finite one-dimensional sample of length >=2')
    max_lag = min(max_lag, len(values)-1)
    centered = values - np.mean(values)
    if not np.any(centered):
        return np.full(max_lag+1, np.nan)
    size = 1 << (2 * len(centered) - 1).bit_length()
    spectrum = np.fft.rfft(centered, size)
    cov = np.fft.irfft(spectrum * spectrum.conjugate(), size)[:max_lag + 1]
    return cov / cov[0]


def pareto_fit(values, min_tail=500):
    """CCDF exponent beta; PDF exponent is beta+1. Fixed candidate grid."""
    x = np.sort(np.asarray(values, dtype=float))
    x = x[np.isfinite(x) & (x > 0)]
    candidates = np.unique(np.quantile(x, [.80, .85, .90, .925, .95, .96, .97, .98, .99, .995]))
    best = None
    for xmin in candidates:
        tail = x[x >= xmin]
        if len(tail) < min_tail:
            continue
        log_ratio = np.log(tail / xmin)
        if log_ratio.sum() <= 0:
            continue
        beta = len(tail) / log_ratio.sum()
        theory = 1 - np.exp(-beta * log_ratio)
        upper = np.arange(1, len(tail) + 1) / len(tail)
        lower = np.arange(len(tail)) / len(tail)
        distance = max(np.max(np.abs(upper - theory)), np.max(np.abs(lower - theory)))
        fit = dict(xmin=float(xmin), beta=float(beta), ks=float(distance), n_tail=len(tail),
                   tail_decades=float(np.log10(tail[-1] / xmin)))
        if best is None or distance < best["ks"]:
            best = fit
    if best is None:
        raise ValueError("not enough positive tail observations")
    return best


def pareto_bootstrap(values, fit, repetitions=200, seed=1, min_tail=500):
    """CSN-style semiparametric iid bootstrap, reselecting xmin each draw.

    P-values have an iid assumption; use thinned samples and separate serial
    diagnostics too. A non-rejection does not establish a power law.
    """
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x) & (x > 0)]
    body = x[x < fit["xmin"]]
    fraction = fit["n_tail"] / len(x)
    rng = np.random.default_rng(seed)
    exceed = 0
    for _ in range(repetitions):
        count = rng.binomial(len(x), fraction)
        synthetic = np.concatenate((rng.choice(body, len(x) - count),
                                    fit["xmin"] * (1 + rng.pareto(fit["beta"], count))))
        simulated = pareto_fit(synthetic, min_tail)
        exceed += simulated["ks"] >= fit["ks"]
    interval = stats.binomtest(exceed, repetitions).proportion_ci()
    return dict(p_iid=float((exceed + 1) / (repetitions + 1)), exceed=int(exceed),
                repetitions=repetitions,
                monte_carlo_ci=[float(interval.low), float(interval.high)])


def tail_alternatives(values, fit):
    """Compare AIC on identical x>=xmin observations; no iid Vuong claims."""
    tail = np.asarray(values, dtype=float)
    tail = tail[tail >= fit["xmin"]]
    xmin, beta = fit["xmin"], fit["beta"]
    y = np.log(tail / xmin)
    pareto_ll = np.sum(np.log(beta) - np.log(tail) - beta * y)
    scale = np.mean(tail - xmin)
    exponential_ll = np.sum(-np.log(scale) - (tail - xmin) / scale)

    # In log-distance y>=0 the truncated lognormal is exp(-a*y-b*y²)/Z.
    # b=0,a>0 contains the exact Pareto limit without an arbitrary mu bound.
    def negative_log_likelihood(parameters):
        a, b = parameters
        if b == 0:
            if a <= 0:
                return np.inf
            log_z = -np.log(a)
        elif a >= 0:
            log_z = .5*np.log(np.pi/b)-np.log(2)+np.log(special.erfcx(a/(2*np.sqrt(b))))
        else:
            log_z = .5*np.log(np.pi/b)+a*a/(4*b)+special.log_ndtr(-a/np.sqrt(2*b))
        return float(len(y)*log_z+a*y.sum()+b*np.square(y).sum()+np.log(tail).sum())

    fits = [optimize.minimize(negative_log_likelihood, start,
                            bounds=[(None, None), (0, None)], method='L-BFGS-B')
            for start in ([beta, 0], [beta, 1], [0, 1/np.mean(y*y)])]
    best = min(fits, key=lambda result: result.fun)
    boundary_ll = -pareto_ll
    use_boundary = boundary_ll <= best.fun
    lognormal_nll = min(boundary_ll, float(best.fun))
    aic = dict(pareto=float(2 - 2 * pareto_ll),
               exponential=float(2 - 2 * exponential_ll),
               truncated_lognormal=float(4 + 2 * lognormal_nll))
    return dict(aic=aic, favored=min(aic, key=aic.get),
                lognormal_converged=bool(use_boundary or best.success),
                lognormal_pareto_boundary=bool(use_boundary or best.x[1] < 1e-8),
                lognormal_parameters=[float(beta),0.] if use_boundary else best.x.tolist(),
                comparison='descriptive AIC, nonregular nested Pareto boundary; no significance claim')


def rs_curve(values, q=20):
    """Lo modified R/S with Bartlett HAC variance, median over blocks.

    large_block_scaling_slope is a sensitivity summary restricted to n>=10(q+1),
    an empirical guard against small-block HAC bias, not a formal Lo test.
    """
    values = np.asarray(values, dtype=float)
    sizes = [128, 256, 512, 1024, 2048, 4096, 8192, 16384]
    sizes = [n for n in sizes if n <= len(values) // 4]
    statistics = []
    block_counts = []
    for size in sizes:
        blocks = values[:len(values) // size * size].reshape(-1, size)
        centered = blocks - blocks.mean(axis=1, keepdims=True)
        variance = np.mean(centered ** 2, axis=1)
        for lag in range(1, min(q, size - 1) + 1):
            cov = np.sum(centered[:, lag:] * centered[:, :-lag], axis=1) / size
            variance += 2 * (1 - lag / (q + 1)) * cov
        cumulative = np.cumsum(centered, axis=1)
        ranges = np.maximum(cumulative.max(axis=1), 0) - np.minimum(cumulative.min(axis=1), 0)
        valid = (variance > 0) & (ranges > 0) & np.isfinite(variance)
        block_counts.append(int(valid.sum()))
        statistics.append(float(np.median(ranges[valid] / np.sqrt(variance[valid]))) if valid.any() else None)
    valid_pairs = [(n,s) for n,s in zip(sizes,statistics) if s is not None and np.isfinite(s)]
    def slope_for(pairs):
        if len(pairs) < 2:
            return None, None
        ns, qs = np.array(pairs).T
        fitted = stats.linregress(np.log(ns), np.log(qs))
        return float(fitted.slope), float(fitted.rvalue**2)
    slope, r_squared = slope_for(valid_pairs)
    large_slope, _ = slope_for([(n,s) for n,s in valid_pairs if n >= 10*(q+1)])
    return dict(sizes=sizes, statistic=statistics,
                beta_n=[float(np.log(s)/np.log(n)) if s is not None else None for n,s in zip(sizes,statistics)],
                scaling_slope=slope, r_squared=r_squared,
                large_block_scaling_slope=large_slope, valid_blocks=block_counts,
                aggregation='median', q=q)


def block_shuffle(values, block_size=60, seed=1):
    """Preserve within-block dependence and remove chronological block order."""
    values = np.asarray(values)
    blocks = values[:len(values) // block_size * block_size].reshape(-1, block_size)
    return blocks[np.random.default_rng(seed).permutation(len(blocks))].ravel()


def describe(returns):
    returns = np.asarray(returns, dtype=float)
    return dict(n=len(returns), zero_fraction=float(np.mean(returns == 0)),
                mean=float(np.mean(returns)), std=float(np.std(returns, ddof=1)),
                skew=float(stats.skew(returns, bias=False)),
                excess_kurtosis=float(stats.kurtosis(returns, bias=False)))
