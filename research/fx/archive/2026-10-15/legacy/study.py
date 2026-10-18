"""Long-run diagnostics for the fixed-size, Poisson-arrival experiment."""

import argparse
import csv
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from time import perf_counter

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import scipy

from ..amm import Account, Pool
from ..diagnostics import (acf, block_shuffle, describe, pareto_bootstrap,
                          pareto_fit, rs_curve, tail_alternatives)
from ..expectation import ExpectationTrader
from ..noise import NoiseTrader
from ..simulation import Simulation


def simulate(kind, seed, ticks):
    trader_class = NoiseTrader if kind == "noise" else ExpectationTrader
    bots = [trader_class(Account(str(i), Decimal("1e12"), Decimal("2e11"))) for i in range(20)]
    sim = Simulation(Pool(Decimal("1000000"), Decimal("200000")), bots,
                     seed=seed, history_limit=30)
    sim.run(ticks, 1)
    if any(r.kind != "trade" for r in sim.records):
        raise ValueError("rejected trades invalidate this experiment")
    initial_gold = Decimal("1000000") + Decimal("1e12") * 20
    initial_foreign = Decimal("200000") + Decimal("2e11") * 20
    assert sim.pool.gold + sim.pool.fees_gold + sum(b.account.gold for b in bots) == initial_gold
    assert sim.pool.foreign + sim.pool.fees_foreign + sum(b.account.foreign for b in bots) == initial_foreign
    prices = np.array([float(p.price) for p in sim.prices])
    gold_flow = np.zeros(ticks + 1)
    for record in sim.records:
        flow = (record.input_amount - record.fee_amount if record.side == "buy"
                else -record.output_amount)
        gold_flow[record.time_seconds] += float(flow)
    gold = 1000000 + np.cumsum(gold_flow)
    if not np.isclose(gold[-1], float(sim.pool.gold), rtol=1e-10):
        raise ValueError("reserve reconstruction failed")
    return prices, gold, len(sim.records)


def analyze(returns, normalized, bootstrap, seed, thin_min_tail=100):
    absolute = np.abs(returns)
    fit = pareto_fit(absolute)
    fit["gof"] = pareto_bootstrap(absolute, fit, bootstrap, seed)
    fit["alternatives"] = tail_alternatives(absolute, fit)
    # Every 60 ticks: weaker short-range dependence, less tail evidence.
    thinned = absolute[::60]
    thin_fit = pareto_fit(thinned, min_tail=thin_min_tail)
    thin_fit["gof"] = pareto_bootstrap(thinned, thin_fit, bootstrap, seed + 1000, min_tail=thin_min_tail)
    hill = {}
    for quantile in (.9, .95, .975, .99, .995):
        positive = absolute[absolute > 0]
        threshold = np.quantile(positive, quantile)
        tail = positive[positive >= threshold]
        hill[str(quantile)] = float(len(tail) / np.log(tail / threshold).sum())
    lag = min(5000, len(returns) // 10)
    correlations = {}
    for name, values in (("returns", returns), ("absolute", absolute),
                         ("normalized_absolute", np.abs(normalized))):
        correlations[name] = acf(values, lag).tolist()
    memory = {}
    for name, values in (("raw", absolute), ("normalized", np.abs(normalized)),
                         ("block_shuffled", block_shuffle(np.abs(normalized), seed=seed))):
        memory[name] = {str(q): rs_curve(values, q) for q in (0, 20, 100)}
    aggregation = {}
    for width in (1, 5, 30, 60, 240):
        sums = returns[:len(returns) // width * width].reshape(-1, width).sum(axis=1)
        aggregation[str(width)] = describe(sums)
    rolling = []
    for block in np.array_split(returns, 10):
        rolling.append(describe(block))
    return dict(distribution=describe(returns), pareto=fit, thinned_pareto=thin_fit,
                hill=hill, correlations=correlations, memory=memory,
                aggregation=aggregation, time_blocks=rolling)


COLORS = {"noise": "#64748b", "expectation": "#2563eb"}


def figures(output, samples, results):
    plt.rcParams.update({"font.size": 10, "axes.spines.top": False,
                         "axes.spines.right": False, "figure.dpi": 150})
    fig, axes = plt.subplots(2, 1, figsize=(11, 7), constrained_layout=True)
    for kind in COLORS:
        for seed, (prices, returns, normalized) in samples[kind].items():
            index = np.linspace(0, len(prices) - 1, min(len(prices), 4000)).astype(int)
            axes[0].plot(index, prices[index], color=COLORS[kind], alpha=.65,
                         label=f"{kind}, seed {seed}")
        returns = next(iter(samples[kind].values()))[1]
        rolling = np.sqrt(np.mean(returns[:len(returns)//1000*1000].reshape(-1, 1000)**2, axis=1))
        axes[1].plot(np.arange(len(rolling))*1000, rolling*100, color=COLORS[kind], label=kind)
    axes[0].set(title="Long-run price evolution: 20 bots, 200 gold/order", ylabel="Gold / foreign")
    axes[0].legend(ncol=2)
    axes[1].set(xlabel="Tick (burn-in excluded in lower panel)", ylabel="1000-tick RMS return (%)")
    axes[1].legend()
    fig.savefig(output / "evolution.png"); plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5), constrained_layout=True)
    tail_lower, tail_upper = [], []
    for kind in COLORS:
        seed = next(iter(samples[kind]))
        returns = samples[kind][seed][1]
        fit = results[kind][str(seed)]["pareto"]
        x = np.sort(np.abs(returns)); x = x[x > 0]
        tail_lower.append(float(np.quantile(x, .8)*100))
        tail_upper.append(float(x[-1]*100))
        idx = np.unique(np.geomspace(1, len(x), 1000).astype(int) - 1)
        axes[0].loglog(x[idx]*100, (len(x)-idx)/len(x), color=COLORS[kind], label=kind)
        xx = np.geomspace(fit["xmin"], x[-1], 100)
        axes[0].loglog(xx*100, fit["n_tail"]/len(x)*(xx/fit["xmin"])**(-fit["beta"]),
                      color=COLORS[kind], linestyle="--")
        hill = results[kind][str(seed)]["hill"]
        axes[1].plot([float(q)*100 for q in hill], list(hill.values()), "o-", color=COLORS[kind], label=kind)
    axes[0].set(title="Upper-tail CCDF; dashed: fitted Pareto", xlabel="Absolute 1-tick return (%)", ylabel="Pr(|return| > x), conditional on >0",
                xlim=(min(tail_lower)*.8, max(tail_upper)*1.1))
    axes[1].set(title="Hill tail index vs threshold", xlabel="Positive-sample threshold percentile", ylabel="CCDF exponent beta")
    axes[0].legend(); axes[1].legend()
    fig.savefig(output / "tails.png"); plt.close(fig)

    fig, axes = plt.subplots(2, 2, figsize=(11, 8), constrained_layout=True)
    for kind in COLORS:
        first = results[kind][next(iter(results[kind]))]
        for name, axis in (("returns", axes[0,0]), ("absolute", axes[0,1]), ("normalized_absolute", axes[1,0])):
            values = np.asarray(first["correlations"][name])
            axis.plot(np.arange(1, 1001), values[1:1001], color=COLORS[kind], label=kind)
            axis.axhline(0, color="#cbd5e1", linewidth=.7)
            axis.set(xlabel="Lag (ticks)", ylabel="Autocorrelation", title=name.replace("_", " "))
        for name, style in (("normalized", "-"), ("block_shuffled", "--")):
            curve = first["memory"][name]["100"]
            axes[1,1].plot(curve["sizes"], curve["beta_n"], style, color=COLORS[kind], label=f"{kind}: {name}")
    for axis in axes.flat:
        axis.legend()
    axes[1,1].set(xscale="log", xlabel="Window (ticks)", ylabel="log(Q) / log(window)", title="Modified R/S, q=100; block shuffle control")
    fig.savefig(output / "dependence.png"); plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticks", type=int, default=200000)
    parser.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    parser.add_argument("--burn-in", type=int, default=2000)
    parser.add_argument("--bootstrap", type=int, default=200)
    parser.add_argument("--output", type=Path, default=Path("research/fx/results/long-run-200"))
    args = parser.parse_args()
    if args.ticks - args.burn_in < 20000 or args.bootstrap < 1:
        parser.error("need at least 20000 post-burn-in ticks and positive bootstrap count")
    args.output.mkdir(parents=True, exist_ok=True)
    metadata = dict(ticks=args.ticks, seeds=args.seeds, burn_in=args.burn_in,
                    bootstrap=args.bootstrap, bots=20, gold_size=200, rate_per_tick=.05,
                    initial_gold=1000000, initial_foreign=200000, fee_rate=.0005,
                    trend_window=30, mean_window=30, trend_strength=30,
                    reversion_strength=.1, noise_sigma=.005, sensitivity=100,
                    numpy=np.__version__, scipy=scipy.__version__, matplotlib=matplotlib.__version__,
                    source_hashes={p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                   for p in Path(__file__).parent.glob("*.py")})
    (args.output / "parameters.json").write_text(json.dumps(metadata, indent=2))
    results = {kind: {} for kind in COLORS}
    samples = {kind: {} for kind in COLORS}
    summary = []
    for kind in COLORS:
        for seed in args.seeds:
            start = perf_counter()
            print(f"simulate {kind} seed={seed}, ticks={args.ticks}", flush=True)
            prices, gold, trades = simulate(kind, seed, args.ticks)
            returns = np.diff(np.log(prices))[args.burn_in:]
            normalized = returns * gold[:-1][args.burn_in:] / 1000000
            np.savez_compressed(args.output / f"{kind}-{seed}.npz", prices=prices, gold=gold)
            print(f"analyze {kind} seed={seed}, trades={trades}", flush=True)
            result = analyze(returns, normalized, args.bootstrap, seed)
            result.update(trades=trades, first_price=float(prices[0]), last_price=float(prices[-1]))
            results[kind][str(seed)] = result
            samples[kind][seed] = (prices, returns, normalized)
            fit = result["pareto"]
            summary.append(dict(model=kind, seed=seed, trades=trades,
                                final_price=prices[-1], excess_kurtosis=result["distribution"]["excess_kurtosis"],
                                tail_beta=fit["beta"], tail_n=fit["n_tail"], tail_decades=fit["tail_decades"],
                                pareto_p_iid=fit["gof"]["p_iid"],
                                thin_p_iid=result["thinned_pareto"]["gof"]["p_iid"],
                                rs_raw_q100=result["memory"]["raw"]["100"]["scaling_slope"],
                                rs_normalized_q100=result["memory"]["normalized"]["100"]["scaling_slope"],
                                rs_shuffled_q100=result["memory"]["block_shuffled"]["100"]["scaling_slope"]))
            (args.output / "diagnostics.json").write_text(json.dumps(results, indent=2))
            print(f"done {kind} seed={seed}: {perf_counter()-start:.1f}s, "
                  f"Pareto beta={fit['beta']:.2f}, p_iid={fit['gof']['p_iid']:.4f}", flush=True)
    with (args.output / "summary.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary[0]))
        writer.writeheader(); writer.writerows(summary)
    figures(args.output, samples, results)
    print(f"saved diagnostics, parameters, raw series and figures: {args.output}", flush=True)


if __name__ == "__main__":
    main()
