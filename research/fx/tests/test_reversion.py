import unittest
from decimal import Decimal as D
import numpy as np

from research.fx.common.amm import Account,Pool
from research.fx.factor_activity.run import load_config
from research.fx.heterogeneous.profiles import make_profiles
from research.fx.episodes.run import simulate as old_simulate
from research.fx.reversion.inventory import inventory_step
from research.fx.reversion.model import simulate


def config():
    return {**load_config(),'ticks':60,'burn_in':0,'entry_quantity':200,'take_profit':.005,
            'stop_loss':.005,'trail_arm':.0025,'trail_drawdown':.0025,
            'inventory_window':40,'inventory_position_cap':500,
            'inventory_sensitivity':100,'inventory_adjustment':.5}


class InventoryTests(unittest.TestCase):
    def test_inventory_adjustment_keeps_a_real_net_position_and_respects_cash(self):
        cfg=config();pool=Pool(D('1000000'),D('200000'),D('0'));a=Account('10',D('5000'),D('1000'))
        event=inventory_step(pool,a,-.05,D('1000'),pool.price,cfg)
        self.assertLess(event['actual_flow'],0)
        self.assertGreater(a.foreign,D('749'))
        self.assertLess(a.foreign,D('760'))
        self.assertGreater(a.gold,D('5000'))
        event=inventory_step(pool,a,.05,D('1000'),pool.price,cfg)
        self.assertGreater(event['actual_flow'],0)
        self.assertGreater(a.foreign,D('1000'))
        self.assertLess(a.foreign,D('1500'))
        self.assertGreaterEqual(a.gold,0)
        self.assertEqual(pool.gold+a.gold,D('1005000'))
        self.assertEqual(pool.foreign+a.foreign,D('201000'))

    def test_disabled_inventory_matches_existing_episode_simulator(self):
        cfg=config();profiles=make_profiles(cfg,301,1)
        old,_,_,_=old_simulate(cfg,profiles,15,.25,81,'trailing')
        new,_,_,_,_=simulate(cfg,profiles,0,.25,81)
        for key in old:self.assertTrue(np.array_equal(old[key],new[key]),key)

    def test_both_shocks_preserve_causal_reference_and_issued_asset_totals(self):
        cfg={**config(),'ticks':30,'order_sigma_foreign':0,'entry_quantity':0}
        profiles=make_profiles(cfg,301,1)
        control,_,_,_,_=simulate(cfg,profiles,5,.25,81,20,0)
        for jump in [.05,-.05]:
            x,s,_,_,_=simulate(cfg,profiles,5,.25,81,20,jump)
            for k in control:self.assertTrue(np.array_equal(x[k][:20],control[k][:20]),k)
            self.assertAlmostEqual(x['slow_reference'][20],5,places=12)
            self.assertAlmostEqual(x['prices'][20]/5-1,jump,places=11)
            self.assertAlmostEqual(x['slow_reference'][21],5*(1+jump)**(1/21),places=11)
            self.assertEqual(D(s['pool_gold'])+sum(D(a['gold']) for a in s['accounts']),D('1100000')+D(s['issued_gold']))
            self.assertEqual(D(s['pool_foreign'])+sum(D(a['foreign']) for a in s['accounts']),D('220000')+D(s['issued_foreign']))


if __name__=='__main__':unittest.main()
