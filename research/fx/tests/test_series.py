import unittest
import numpy as np
from research.fx.common.analysis import state_diagnostics


class SeriesTests(unittest.TestCase):
    def test_next_block_relationship_is_distinct_from_same_block(self):
        # Retained from the previous branch's time-alignment regression.
        block_w=np.array([1.,4.,2.,5.,3.]*34)
        predicted=np.repeat(block_w,30)*1e-8
        returns=np.sqrt(np.repeat(np.r_[6.,block_w[:-1]],30))*1e-4
        row=state_diagnostics(predicted,returns)['blocks'][0]
        self.assertAlmostEqual(row['next_block_corr'],1,places=10)
        self.assertLess(row['same_block_corr'],0)
        self.assertEqual(row['next_pairs'],169)
