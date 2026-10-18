import unittest
from decimal import Decimal
from research.fx.common.amm import Account, Pool
from research.fx.common.execution import execute


class ExecutionTests(unittest.TestCase):
    def test_execution_clips_live_wallet_and_conserves_both_assets(self):
        pool = Pool(Decimal('1000'), Decimal('200'), Decimal(0))
        account = Account('a', Decimal('5'), Decimal('1'))
        initial = (pool.gold + account.gold, pool.foreign + account.foreign)
        self.assertIsNotNone(execute(pool, account, 100, .005))
        self.assertGreaterEqual(account.gold, 0)
        self.assertLess(account.foreign, Decimal('2'))
        self.assertIsNotNone(execute(pool, account, -100, .5))
        self.assertEqual(account.foreign, 0)
        self.assertEqual((pool.gold + account.gold, pool.foreign + account.foreign), initial)
