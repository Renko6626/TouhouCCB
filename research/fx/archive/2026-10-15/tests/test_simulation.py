"""Core accounting and clock contracts, not strategy tests."""

import unittest
from dataclasses import asdict
from decimal import Decimal as D

from research.fx.amm import Account, Order, Pool
from research.fx.bots import Bot
from research.fx.simulation import Injection, Simulation


class ProbeBot(Bot):
    """Test-only orders exercise the clock and supplied random source."""

    def decide(self, view, rng):
        return Order("buy", D("10")) if rng.random() < 0.5 else Order("sell", D("10"))


class SimulationTests(unittest.TestCase):
    def test_trades_conserve_each_currency_including_fees(self):
        pool = Pool(D("1000000"), D("5000000"))
        account = Account("trader", D("500"), D("100"))
        initial_gold = pool.gold + account.gold
        initial_foreign = pool.foreign + account.foreign
        for order in (Order("buy", D("100")), Order("sell", D("50"))):
            pool.trade(account, order)
            self.assertEqual(pool.gold + account.gold + pool.fees_gold, initial_gold)
            self.assertEqual(pool.foreign + account.foreign + pool.fees_foreign,
                             initial_foreign)
            self.assertGreaterEqual(account.gold, 0)
            self.assertGreaterEqual(account.foreign, 0)
        self.assertGreater(pool.fees_gold, 0)
        self.assertGreater(pool.fees_foreign, 0)

    def test_insufficient_balance_does_not_change_pool_or_account(self):
        pool = Pool(D("1000000"), D("5000000"))
        account = Account("trader", D("5"), D("2"))
        before = (asdict(pool), asdict(account))
        for order in (Order("buy", D("10")), Order("sell", D("10"))):
            with self.assertRaisesRegex(ValueError, "insufficient"):
                pool.trade(account, order)
            self.assertEqual((asdict(pool), asdict(account)), before)

    def test_injection_changes_price_before_decision_without_trade_volume(self):
        views = []

        class Observer(Bot):
            def decide(self, view, rng):
                views.append(view)
                return None

        pool = Pool(D("1000000"), D("5000000"))
        sim = Simulation(pool, [Observer(Account("observer"))])
        sim.run(120, 60, (Injection(60, gold=D("100000")),))
        self.assertEqual([p.price for p in sim.prices], [D("0.2"), D("0.22"), D("0.22")])
        self.assertEqual(views[0].price, D("0.2"))
        self.assertEqual(views[1].price, D("0.22"))
        self.assertEqual([p.time_seconds for p in views[1].history], [0])
        self.assertEqual(sim.records[0].kind, "injection")
        self.assertEqual(sim.records[0].gold_volume, 0)
        self.assertEqual(pool.gold, D("1100000"))
        self.assertEqual(pool.foreign, D("5000000"))

    def test_seed_reproduces_orders_and_results(self):
        def run(seed):
            bot = ProbeBot(Account("probe", D("500"), D("100")))
            sim = Simulation(Pool(D("1000000"), D("5000000")), [bot], seed=seed)
            sim.run(600, 60)
            return sim.prices, sim.records, asdict(bot.account)

        self.assertEqual(run(7), run(7))
        self.assertNotEqual(run(7), run(8))


if __name__ == "__main__":
    unittest.main()
