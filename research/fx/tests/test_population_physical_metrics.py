import unittest

import numpy as np

from research.fx.heterogeneous.profiles import make_profiles
from research.fx.reversion.repeated.entry_model import simulate
from research.fx.reversion.repeated.population_model import replicate_profiles
from research.fx.reversion.repeated.population_time_metrics import metrics
from research.fx.tests.test_reversion import config


class PopulationPhysicalMetricsTests(unittest.TestCase):
    def test_short_wallet_slice_and_profit_denominator_follow_population(self):
        original=make_profiles(config(),301,1)
        for n in [20,40]:
            cfg={**config(),'accounts':n,'ticks':1000,'tau':540,'inventory_window':360,'psi':.0008}
            x,s,episodes,_,_,_=simulate(cfg,replicate_profiles(original,n),n//4,.25,81,{},'weak_trend',dict(trend_multiplier=.25))
            d=metrics(x,s,episodes,5)
            self.assertGreater(s['behavior']['completed'],0)
            self.assertAlmostEqual(d['short_realized_per_account'],float(s['behavior']['realized_pnl'])/(n//4))
            expected=x['account_gold'][-1,3*n//4:]+x['account_foreign'][-1,3*n//4:]*x['prices'][-1]
            self.assertAlmostEqual(d['short_mean_wealth'],float(np.mean(expected)))
            self.assertEqual(d['range30']['ticks_per_window'],360)


if __name__=='__main__':unittest.main()
