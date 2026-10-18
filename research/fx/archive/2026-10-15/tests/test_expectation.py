"""Three-factor signal composition and tick-driven forecast updates."""

import math
import unittest
from decimal import Decimal as D
from random import Random

from research.fx.amm import Account, Pool
from research.fx.bots import MarketView, PricePoint
from research.fx.expectation import ExpectationTrader
from research.fx.simulation import Simulation


class ExpectationTests(unittest.TestCase):
    def test_three_factors_form_log_price_gap(self):
        bot = ExpectationTrader(Account("mixed"), trend_window=2, mean_window=2,
                                trend_strength=2, reversion_strength=0.5,
                                noise_sigma=0)
        history = (PricePoint(0, D("4")), PricePoint(60, D("5")))
        view = MarketView(120, D("6"), history, D(0), D(0), tick=2)
        bot.decide(view, Random(1))
        # Trend extrapolates the 4->6 move; geometric mean reference is sqrt(20).
        expected = math.log(1.5) + 0.5 * math.log(math.sqrt(20) / 6)
        self.assertAlmostEqual(bot.expected_log_gap, expected)
        self.assertAlmostEqual(float(bot.target_price), 6 * math.exp(expected))
        self.assertGreater(bot.buy_probability(view), 0.5)

    def test_forecasts_update_without_orders_and_seed_reproduces_run(self):
        bot = ExpectationTrader(Account("mixed"), rate_per_tick=0,
                                trend_strength=0, reversion_strength=0, noise_sigma=0.01)
        rng = Random(7)
        view = MarketView(0, D("5"), (), D(0), D(0), tick=0)
        self.assertEqual(bot.decide(view, rng), [])
        first = bot.target_price
        self.assertEqual(bot.decide(view, rng), [])
        self.assertNotEqual(first, bot.target_price)

        def run():
            trader = ExpectationTrader(Account("mixed", D("1e12"), D("2e11")),
                                       rate_per_tick=2)
            sim = Simulation(Pool(D("1000000"), D("200000")), [trader], seed=7)
            sim.run(600, 60)
            return sim.records
        records = run()
        self.assertEqual(records, run())
        self.assertTrue(records)
        buys = [r for r in records if r.side == "buy"]
        self.assertTrue(buys)
        self.assertTrue(all(r.input_amount == D("200") for r in buys))


if __name__ == "__main__":
    unittest.main()
