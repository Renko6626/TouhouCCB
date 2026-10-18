"""Window selection and observable trading bias for trend followers."""

import unittest
from decimal import Decimal as D
from random import Random

from research.fx.amm import Account
from research.fx.bots import MarketView, PricePoint
from research.fx.trend import TrendTrader


class TrendTests(unittest.TestCase):
    def test_window_return_sets_sigmoid_probability_and_neutral_warmup(self):
        bot = TrendTrader(Account("trend"), lookback_ticks=2, sensitivity=100)
        history = (PricePoint(0, D("5")), PricePoint(60, D("9")))
        def probability(price, past=history):
            return bot.buy_probability(MarketView(120, D(price), past, D(0), D(0), tick=2))
        self.assertAlmostEqual(probability("5.05"), 0.7310585786300049)
        self.assertAlmostEqual(probability("4.95"), 0.2689414213699951)
        self.assertEqual(probability("5"), 0.5)
        self.assertEqual(probability("5.05", history[:1]), 0.5)
        self.assertEqual(probability("50000000"), 1.0)

    def test_orders_follow_the_probability_without_changing_size(self):
        bot = TrendTrader(Account("trend", D("1e12"), D("2e11")),
                          lookback_ticks=1, sensitivity=100,
                          rate_per_tick=100, gold_size=D("500"))
        rng = Random(7)
        bot.decide(MarketView(0, D("5"), (), D(0), D(0), tick=0), rng)
        view = MarketView(60, D("5.1"), (PricePoint(0, D("5")),),
                          D(0), D(0), tick=1)
        orders = bot.decide(view, rng)
        buys = [order for order in orders if order.side == "buy"]
        self.assertGreater(len(orders), 50)
        self.assertGreater(len(buys) / len(orders), 0.7)
        self.assertTrue(all(order.amount == D("500") for order in buys))


if __name__ == "__main__":
    unittest.main()
