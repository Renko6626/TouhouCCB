import unittest
import numpy as np
from research.fx.impact_study import inventory_horizons, persistence


class ImpactTests(unittest.TestCase):
    def test_opposing_inventory_changes_cancel_through_covariance(self):
        x = dict(inventory_strategy=np.array([0.,1.,0.,1.]),
                 inventory_noise=np.array([0.,-1.,0.,-1.]),
                 prices=np.ones(4)*5, pool_foreign=np.ones(4)*200000)
        row = inventory_horizons(x,0,widths=[1])[0]
        self.assertAlmostEqual(row['strategy_var'],4/3)
        self.assertAlmostEqual(row['noise_var'],4/3)
        self.assertAlmostEqual(row['two_cov'],-8/3)
        self.assertEqual(row['net_inventory_var'],0)

    def test_next_block_relationship_is_distinct_from_same_block(self):
        block_w = np.array([1.,4.,2.,5.,3.]*34)
        predicted = np.repeat(block_w,30)*1e-8
        returns = np.sqrt(np.repeat(np.r_[6.,block_w[:-1]],30))*1e-4
        x = dict(prices=np.exp(np.r_[0.,np.cumsum(returns)]),
                 predicted_second_moment=np.r_[0.,predicted],
                 predicted_mean=np.zeros(len(predicted)+1))
        row = persistence(x,0)['blocks'][0]
        self.assertAlmostEqual(row['next_block_corr'],1,places=10)
        self.assertLess(row['same_block_corr'],0)
        self.assertEqual(row['next_pairs'],169)
