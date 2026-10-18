import unittest
from decimal import Decimal as D
import numpy as np

from research.fx.tests.test_reversion import config
from research.fx.heterogeneous.profiles import make_profiles
from research.fx.reversion.model import simulate as old_simulate
from research.fx.reversion.repeated.model import simulate


class RepeatedScenarioTests(unittest.TestCase):
    def test_empty_schedule_preserves_strategy_and_individual_wallet_marks(self):
        cfg=config();profiles=make_profiles(cfg,301,1)
        old,_,_,_,_=old_simulate(cfg,profiles,5,.25,81)
        x,s,_,_,_=simulate(cfg,profiles,5,.25,81,{})
        for key in old:self.assertTrue(np.array_equal(old[key],x[key]),key)
        np.testing.assert_allclose(x['account_gold'].sum(axis=1),x['random_gold']+x['reversion_gold']+x['factor_gold'],atol=1e-8)
        np.testing.assert_allclose(x['account_wealth'],x['account_gold']+x['account_foreign']*x['prices'][:,None],atol=1e-8)
        for i,a in enumerate(s['accounts']):
            self.assertEqual(x['account_foreign'][-1,i],float(a['foreign']))
            self.assertEqual(x['account_gold'][-1,i],float(a['gold']))

    def test_repeated_shocks_revalue_actual_wallets_and_keep_terminal_event(self):
        cfg={**config(),'ticks':30};profiles=make_profiles(cfg,301,1)
        control,_,_,_,_=simulate(cfg,profiles,5,.25,81,{})
        x,s,_,_,_=simulate(cfg,profiles,5,.25,81,{10:.05,20:-.05,30:.05})
        for k in control:self.assertTrue(np.array_equal(x[k][:10],control[k][:10]),k)
        self.assertEqual([e['tick'] for e in s['events']],[10,20,30])
        for e in s['events']:
            pre,post=D(e['pre_price']),D(e['post_price'])
            self.assertAlmostEqual(float(post/pre-1),e['return_requested'],places=11)
            for a in e['accounts']:
                self.assertAlmostEqual(float(D(a['wealth_after'])-D(a['wealth_before'])),float(D(a['foreign'])*(post-pre)),places=9)
                self.assertEqual(float(a['foreign']),x['account_foreign'][e['tick'],int(a['name'])])
        np.testing.assert_allclose(x['gold']+x['account_gold'].sum(axis=1),1100000+np.cumsum(x['injection_gold']),atol=1e-8)
        np.testing.assert_allclose(x['pool_foreign']+x['account_foreign'].sum(axis=1),220000+np.cumsum(x['injection_foreign']),atol=1e-8)
        self.assertEqual(len(x['prices']),31)


if __name__=='__main__':unittest.main()
