import unittest
from decimal import Decimal
import numpy as np
from research.fx.amm import Account, Pool
from research.fx.switching import Agent, hysteresis, execute
from research.fx.switching_run import simulate, read_config


class SwitchingTests(unittest.TestCase):
    def test_hysteresis_hold_exit_and_strong_reverse(self):
        self.assertEqual(hysteresis(0, .0005, .0005, .00025), 0)
        self.assertEqual(hysteresis(0, .0006, .0005, .00025), 1)
        self.assertEqual(hysteresis(1, .0003, .0005, .00025), 1)
        self.assertEqual(hysteresis(1, .0002, .0005, .00025), 0)
        self.assertEqual(hysteresis(1, -.0006, .0005, .00025), -1)
        self.assertEqual(hysteresis(-1, -.0003, .0005, .00025), -1)
        self.assertEqual(hysteresis(-1, -.0002, .0005, .00025), 0)

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

    def test_score_uses_previous_exposure(self):
        agent = Agent(0, 'M', False, 41, read_config(), 5)
        agent.shadow_u[:] = [1, -1]
        agent.observe(.01, [0., .01], .01)
        np.testing.assert_allclose(agent.scores, [.01 * agent.gamma, -.01 * agent.gamma])
        np.testing.assert_array_equal(agent.shadow_u, [1, -1])

    def test_paired_random_inputs_and_conservation(self):
        cfg = {**read_config(), 'ticks': 500, 'burn_in': 50}
        a, sa = simulate(cfg, 'fixed', 41, 0)
        b, sb = simulate(cfg, 'performance', 41, 1e6)
        np.testing.assert_array_equal(a['arrivals'], b['arrivals'])
        np.testing.assert_array_equal(a['noise_demand'], b['noise_demand'])
        self.assertTrue(sa['conservation'] and sb['conservation'])
        self.assertEqual(sa['switches'], 0)
        self.assertGreater(sb['choice_opportunities'], 0)


if __name__ == '__main__':
    unittest.main()
