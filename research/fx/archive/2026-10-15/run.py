"""Run fixed-size Poisson orders from noise, trend or mixed expectations."""

import argparse
from decimal import Decimal

from .amm import Account, Pool
from .expectation import ExpectationTrader
from .noise import NoiseTrader
from .simulation import Injection, Simulation
from .trend import TrendTrader


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=int, default=21600, help="simulated seconds")
    parser.add_argument("--step", type=int, default=60, help="clock step in seconds")
    parser.add_argument("--ticks", type=int, help="number of intervals; overrides --duration")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--output", default="research/fx/results/demo")
    parser.add_argument("--inject-at", type=int, default=3600)
    parser.add_argument("--inject-gold", type=Decimal, default=Decimal("0"))
    parser.add_argument("--inject-foreign", type=Decimal, default=Decimal("0"))
    parser.add_argument("--noise-bots", type=int, default=20)
    parser.add_argument("--noise-rate", type=float, default=0.05,
                        help="expected orders per bot per tick")
    parser.add_argument("--noise-size", type=Decimal, default=Decimal("200"),
                        help="gold-equivalent input per order")
    parser.add_argument("--fee-rate", type=Decimal, default=Decimal("0.0005"))
    parser.add_argument("--trend-bots", type=int, default=0)
    parser.add_argument("--trend-window", type=int, default=30, help="lookback in ticks")
    parser.add_argument("--trend-sensitivity", type=float, default=100)
    parser.add_argument("--trend-rate", type=float, default=0.05)
    parser.add_argument("--trend-size", type=Decimal, default=Decimal("200"))
    parser.add_argument("--expectation-bots", type=int, default=0)
    parser.add_argument("--expectation-window", type=int, default=30)
    parser.add_argument("--expectation-trend-strength", type=float, default=30)
    parser.add_argument("--expectation-reversion-strength", type=float, default=0.1)
    parser.add_argument("--expectation-noise-sigma", type=float, default=0.005)
    parser.add_argument("--expectation-sensitivity", type=float, default=100)
    parser.add_argument("--expectation-rate", type=float, default=0.05)
    parser.add_argument("--expectation-size", type=Decimal, default=Decimal("200"))
    args = parser.parse_args()
    if min(args.noise_bots, args.trend_bots, args.expectation_bots) < 0:
        parser.error("bot counts must be nonnegative")
    pool = Pool(Decimal("1000000"), Decimal("200000"), args.fee_rate)
    bots = [NoiseTrader(Account(f"noise-{i + 1}", Decimal("1e12"), Decimal("2e11")),
                        rate_per_tick=args.noise_rate, gold_size=args.noise_size)
            for i in range(args.noise_bots)]
    bots.extend(TrendTrader(Account(f"trend-{i + 1}", Decimal("1e12"), Decimal("2e11")),
                           lookback_ticks=args.trend_window,
                           sensitivity=args.trend_sensitivity,
                           rate_per_tick=args.trend_rate, gold_size=args.trend_size)
                for i in range(args.trend_bots))
    bots.extend(ExpectationTrader(
        Account(f"expectation-{i + 1}", Decimal("1e12"), Decimal("2e11")),
        trend_window=args.expectation_window, mean_window=args.expectation_window,
        trend_strength=args.expectation_trend_strength,
        reversion_strength=args.expectation_reversion_strength,
        noise_sigma=args.expectation_noise_sigma, sensitivity=args.expectation_sensitivity,
        rate_per_tick=args.expectation_rate, gold_size=args.expectation_size)
        for i in range(args.expectation_bots))
    simulation = Simulation(pool, bots, seed=args.seed)
    injections = ()
    if args.inject_gold != 0 or args.inject_foreign != 0:
        injections = (Injection(args.inject_at, args.inject_gold, args.inject_foreign),)
    duration = args.duration if args.ticks is None else args.ticks * args.step
    simulation.run(duration, args.step, injections)
    simulation.write_csv(args.output)
    chart = simulation.write_chart(args.output)
    trades = sum(r.kind == "trade" for r in simulation.records)
    print(f"samples={len(simulation.prices)} trades={trades} events={len(simulation.records)} "
          f"final_price={pool.price} output={args.output} chart={chart}")


if __name__ == "__main__":
    main()
