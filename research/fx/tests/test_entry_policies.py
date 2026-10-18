import unittest

import numpy as np

from research.fx.heterogeneous.profiles import make_profiles
from research.fx.reversion.repeated.entry_model import flow_confirmation, simulate
from research.fx.reversion.repeated.population_model import replicate_profiles, simulate as baseline
from research.fx.tests.test_reversion import config


SETTINGS=dict(trend_multiplier=.25,confirmation_window=30,confirmation_min_accounts=3,
              confirmation_min_fraction=2/3)


class EntryPolicyTests(unittest.TestCase):
    def test_original_preserves_all_paths_and_ledgers(self):
        cfg={**config(),'accounts':40,'ticks':60,'psi':.0008}
        profiles=replicate_profiles(make_profiles(config(),301,1),40)
        old=baseline(cfg,profiles,10,.25,81,{})
        new=simulate(cfg,profiles,10,.25,81,{},'original',SETTINGS)
        for key in old[0]:np.testing.assert_array_equal(new[0][key],old[0][key],err_msg=key)
        self.assertEqual(new[1:5],old[1:])

    def test_confirmation_excludes_self_and_current_or_future_orders(self):
        f=np.full((40,5),1000.)
        f[15:30,1:4]+=10;f[30:,1:4]+=20
        passed,details=flow_confirmation(f,31,0,1,SETTINGS)
        self.assertTrue(passed)
        f[31:]=0  # Unfinished tick and future rows cannot alter the decision.
        self.assertEqual(flow_confirmation(f,31,0,1,SETTINGS),(passed,details))
        self.assertFalse(flow_confirmation(f,31,0,-1,SETTINGS)[0])
        only_self=np.full((40,5),1000.);only_self[15:30,0]+=100;only_self[30:,0]+=200
        self.assertFalse(flow_confirmation(only_self,31,0,1,SETTINGS)[0])
        reversed_flow=np.full((40,5),1000.)
        reversed_flow[15:30,1:4]+=30;reversed_flow[30:,1:4]+=20
        self.assertFalse(flow_confirmation(reversed_flow,31,0,1,SETTINGS)[0])

    def test_entry_changes_leave_exits_and_conservation_in_place(self):
        cfg={**config(),'accounts':40,'ticks':300,'psi':.0008}
        profiles=replicate_profiles(make_profiles(config(),301,1),40)
        for policy in ['weak_trend','confirmed_trend','random_direction']:
            x,s,episodes,fills,_,decisions=simulate(cfg,profiles,10,.25,81,{},policy,SETTINGS)
            self.assertGreater(len(episodes),0)
            for fill in fills:
                self.assertEqual(float(fill['quantity']),200)
                if fill['action']=='close':
                    self.assertIn(fill['reason'],['take_profit','stop_loss','trailing'])
            np.testing.assert_allclose(x['gold']+x['account_gold'].sum(axis=1),1200000,atol=1e-8,rtol=0)
            np.testing.assert_allclose(x['pool_foreign']+x['account_foreign'].sum(axis=1),240000,atol=1e-8,rtol=0)
            self.assertTrue((x['account_gold']>=0).all() and (x['account_foreign']>=0).all())
            if policy=='confirmed_trend':
                self.assertTrue(any(d['action']=='veto' for d in decisions))
                for d in decisions:
                    if d['action']=='open' and d['dominant']=='trend':self.assertTrue(d['accepted'])


if __name__=='__main__':unittest.main()
