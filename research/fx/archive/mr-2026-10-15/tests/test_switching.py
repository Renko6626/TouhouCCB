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
        held = agent.shadow_u.copy()
        agent.signals = [.001, -.001]
        agent.wake()  # New exposures were not held at the interval start.
        agent.score(.01, held)
        np.testing.assert_array_equal(agent.scores, [0, 0])
        held = agent.shadow_u.copy()
        agent.signals = [0., 0.]
        agent.wake()  # Exiting now must not erase the prior held exposure.
        agent.score(.01, held)
        np.testing.assert_allclose(agent.scores, [.002 * agent.gamma, -.002 * agent.gamma])

    def test_next_wake_gap_uses_new_signal_without_changing_state(self):
        agent = Agent(0, 'M', False, 41, read_config(), 5)
        agent.signals = [.001, 0.]
        self.assertEqual(agent.intent_gap(), 500)
        self.assertEqual(agent.state, 0)
        self.assertEqual(agent.account.foreign, Decimal('1000'))

    def test_smaller_amplitude_keeps_wallet_and_baseline_inventory(self):
        for amplitude, quantity in [(125, 25), (250, 50), (500, 100)]:
            cfg = {**read_config(), 'position_cap': amplitude}
            agent = Agent(0, 'M', False, 41, cfg, 5)
            self.assertEqual(agent.account.gold, Decimal('5000'))
            self.assertEqual(agent.account.foreign, Decimal('1000'))
            agent.signals = [.001, 0.]
            self.assertEqual(agent.wake(), quantity)

    def test_inventory_decomposition_and_matched_noise(self):
        cfg = {**read_config(), 'ticks': 500, 'burn_in': 50}
        fixed, _ = simulate(cfg, 'fixed', 41, 0)
        matched, summary = simulate(cfg, 'matched_noise', 41, 0)
        np.testing.assert_array_equal(fixed['arrivals'], matched['arrivals'])
        np.testing.assert_array_equal(fixed['noise_demand'], matched['noise_demand'])
        np.testing.assert_allclose(fixed['pool_foreign'],
            200000-fixed['inventory_strategy']-fixed['inventory_noise'], rtol=0, atol=1e-8)
        self.assertTrue(summary['conservation'])
        np.testing.assert_array_equal(matched['inventory_strategy'], np.zeros(501))
        self.assertEqual(len([a for a in summary['accounts'] if a['kind']=='I']), 16)

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
