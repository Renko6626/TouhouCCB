"""Measure realized weighted forecast factors in a short unchanged run."""

import argparse
import csv
import json
from decimal import Decimal
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from scipy.special import expit

from ..amm import Account, Pool
from ..expectation import ExpectationTrader
from ..simulation import Simulation

FACTORS = ("trend", "reversion", "noise")


class ObservedTrader(ExpectationTrader):
    def __init__(self, account, rows):
        super().__init__(account)
        self.rows = rows

    def decide(self, view, rng):
        orders = super().decide(view, rng)
        self.rows.append(dict(tick=view.tick, bot=self.account.name,
                              trend=self.trend_component,
                              reversion=self.reversion_component,
                              noise=self.noise_component, gap=self.expected_log_gap,
                              price=float(view.price), target=float(self.target_price),
                              p_buy=self.buy_probability(view), orders=len(orders),
                              buys=sum(order.side == "buy" for order in orders)))
        return orders


def summarize(rows, order_weighted=False):
    weights = np.array([row["orders"] if order_weighted else 1 for row in rows], dtype=float)
    weights /= weights.sum()
    result = {}
    for name in FACTORS:
        values = np.array([row[name] for row in rows])
        mean = np.sum(weights * values)
        result[name] = dict(mean=float(mean), std=float(np.sqrt(np.sum(weights*(values-mean)**2))),
                            rms=float(np.sqrt(np.sum(weights*values**2))),
                            mean_absolute=float(np.sum(weights*np.abs(values))))
    deterministic = np.array([row["trend"]+row["reversion"] for row in rows])
    total = np.array([row["gap"] for row in rows])
    meaningful = np.abs(deterministic) > 1e-12
    result["signal_sign_flip_fraction"] = float(
        np.sum(weights * meaningful * (np.sign(total) != np.sign(deterministic)))
        / np.sum(weights * meaningful))
    result["mean_absolute_probability_change"] = float(np.sum(weights*np.abs(expit(100*total)-expit(100*deterministic))))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticks", type=int, default=360)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--output", type=Path, default=Path("research/fx/results/factors-200"))
    args = parser.parse_args()
    if args.ticks <= 30:
        parser.error("need more than 30 ticks to observe all factors")
    rows = []
    bots = [ObservedTrader(Account(str(i), Decimal("1e12"), Decimal("2e11")), rows)
            for i in range(20)]
    sim = Simulation(Pool(Decimal("1000000"), Decimal("200000")), bots,
                     seed=args.seed, history_limit=30)
    sim.run(args.ticks*60, 60)
    args.output.mkdir(parents=True, exist_ok=True)
    sim.write_csv(args.output)
    sim.write_chart(args.output)
    with (args.output / "factors.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)
    ready = [row for row in rows if row["tick"] >= 30]
    tick_groups = [[row for row in ready if row["tick"] == tick]
                   for tick in range(30, args.ticks+1)]
    group_means = {name: [np.mean([row[name] for row in group]) for group in tick_groups]
                   for name in FACTORS}
    statistics = dict(parameters=dict(ticks=args.ticks, seed=args.seed, bots=20,
        order_size=200, rate_per_tick=.05, trend_strength=30, reversion_strength=.1,
        noise_sigma=.005, sensitivity=100, trend_window=30, mean_window=30),
        total_updates=len(rows), analyzed_updates=len(ready),
        analyzed_orders=sum(row["orders"] for row in ready),
        all_updates=summarize(ready), at_order_arrivals=summarize(ready, True),
        group_mean_std={name: float(np.std(values)) for name,values in group_means.items()})
    (args.output / "statistics.json").write_text(json.dumps(statistics, indent=2))

    fig, axes = plt.subplots(1, 2, figsize=(10, 4), constrained_layout=True)
    labels = ["Trend", "Mean reversion", "Forecast noise"]
    for axis,scope,title in zip(axes, ("all_updates", "at_order_arrivals"),
                                ("All forecasts after warm-up", "Weighted by order arrivals")):
        heights = [statistics[scope][name]["std"]*100 for name in FACTORS]
        axis.bar(labels, heights, color="#2563eb")
        for x,height in enumerate(heights):
            axis.text(x,height,f"{height:.4f}%",ha="center",va="bottom")
        axis.set(title=title, ylabel="Weighted log-price contribution std (%)", ylim=(0,max(heights)*1.2))
        axis.spines[["top","right"]].set_visible(False)
    fig.savefig(args.output / "factor-scales.png", dpi=150); plt.close(fig)
    print(json.dumps(statistics, indent=2))


if __name__ == "__main__":
    main()
