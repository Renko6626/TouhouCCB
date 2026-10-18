"""The external issue changes reserves, not trading volume or policy rules."""
import math
import unittest
import numpy as np

from research.fx.injection.run import simulate
from research.fx.mixed.run import simulate as original, load_config, make_profiles


class InjectionTests(unittest.TestCase):
    def test_zero_injection_preserves_existing_mixed_path(self):
        cfg = {**load_config(), 'ticks': 200, 'burn_in': 30}
        profiles = make_profiles(cfg, 301, 1)
        for b in (0, .25):
            old, _ = original(cfg, profiles, 10, b, 81)
            new, _ = simulate(cfg, profiles, 10, b, 81, 100, 0)
            for key in old:
                np.testing.assert_array_equal(new[key], old[key], err_msg=key)

    def test_issue_changes_price_and_next_activity_without_trading(self):
        cfg = {**load_config(), 'ticks': 20, 'burn_in': 0, 'order_sigma_foreign': 0}
        profiles = make_profiles(cfg, 301, 1)
        x, summary = simulate(cfg, profiles, 20, .25, 81, 10, 100000)
        np.testing.assert_array_equal(x['prices'][:10], np.full(10, 5.))
        np.testing.assert_array_equal(x['prices'][10:], np.full(11, 5.5))
        self.assertEqual(x['trades'].sum(), 0)
        self.assertEqual(x['gold_volume'].sum(), 0)
        self.assertEqual(x['executed_abs_quantity'].sum(), 0)
        self.assertEqual(x['injection_gold'].sum(), 100000)
        self.assertEqual(summary['pool_gold'], '1100000')
        self.assertTrue(summary['conserved_with_issue'])
        jump = math.log(1.1)
        self.assertAlmostEqual(x['returns'][10], jump)
        a = math.exp(-1/cfg['tau'])
        expected_z = math.sqrt(1-a*a)*jump/cfg['psi']
        self.assertAlmostEqual(x['z_pre'][11], expected_z)
        self.assertAlmostEqual(x['intensity'][11], .75+.25*expected_z**2)
        self.assertTrue(all(a['gold']=='5000' and a['foreign']=='1000' for a in summary['accounts']))
