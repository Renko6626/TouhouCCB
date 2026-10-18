"""Arrival statistics and tick integration for the noise baseline."""

import math
import unittest
from collections import Counter
from decimal import Decimal as D
from random import Random

from research.fx.amm import Account, Pool
from research.fx.bots import MarketView
from research.fx.noise import NoiseTrader
from research.fx.simulation import Simulation


class NoiseTests(unittest.TestCase):
    def test_poisson_arrivals_include_empty_and_multiple_order_ticks(self):
        bot = NoiseTrader(Account("noise", D("1e12"), D("2e11")),
                          rate_per_tick=2, gold_size=D("500"))
        rng = Random(123)
        counts = Counter()
        buys = sells = 0
        ticks = 10000
        for tick in range(ticks + 1):
            view = MarketView(tick, D("5"), (), bot.account.gold,
                              bot.account.foreign, tick=tick)
            orders = bot.decide(view, rng)
            if tick == 0:
                self.assertEqual(orders, [])
                continue
            counts[len(orders)] += 1
            for order in orders:
                if order.side == "buy":
                    buys += 1
                    self.assertEqual(order.amount, D("500"))
                else:
                    sells += 1
                    self.assertEqual(order.amount, D("100"))
        total = buys + sells
        self.assertAlmostEqual(total / ticks, 2, delta=0.08)
        self.assertAlmostEqual(counts[0] / ticks, math.exp(-2), delta=0.02)
        self.assertGreater(sum(n for k, n in counts.items() if k > 1), 0)
        self.assertAlmostEqual(buys / total, 0.5, delta=0.03)

    def test_simulation_executes_all_arrivals_and_reproduces_seed(self):
        def run():
            bot = NoiseTrader(Account("noise", D("1e12"), D("2e11")),
                              rate_per_tick=4, gold_size=D("500"))
            sim = Simulation(Pool(D("1000000"), D("200000")), [bot], seed=7)
            sim.run(600, 60)
            return sim

        sim = run()
        self.assertEqual(sim.records, run().records)
        trades = [r for r in sim.records if r.kind == "trade"]
        self.assertEqual(len(trades), len(sim.records))
        self.assertGreater(len(trades), 10)
        self.assertGreater(max(Counter(r.time_seconds for r in trades).values()), 1)
        self.assertEqual(len(sim.prices), 11)
        self.assertNotEqual(sim.prices[-1].price, D("5"))


if __name__ == "__main__":
    unittest.main()
