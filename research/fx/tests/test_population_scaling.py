import unittest
from decimal import Decimal

import numpy as np

from research.fx.heterogeneous.profiles import make_profiles
from research.fx.reversion.repeated.model import simulate as original_simulate
from research.fx.reversion.repeated.population_model import replicate_profiles, simulate
from research.fx.tests.test_reversion import config


class PopulationScalingTests(unittest.TestCase):
    def test_twenty_accounts_preserve_every_original_series(self):
        cfg={**config(),'ticks':30,'psi':.0008}
        profiles=make_profiles(cfg,301,1)
        schedule={10:.05,20:-.05,30:.05}
        old=original_simulate(cfg,profiles,5,.25,81,schedule)
        new=simulate(cfg,replicate_profiles(profiles,20),5,.25,81,schedule)
        for key in old[0]:
            np.testing.assert_array_equal(new[0][key],old[0][key],err_msg=key)
        self.assertEqual(new[1:],old[1:])

    def test_eighty_accounts_have_scaled_roles_and_conserve_their_actual_capital(self):
        cfg={**config(),'ticks':30,'psi':.0008,'accounts':80}
        profiles=replicate_profiles(make_profiles(config(),301,1),80)
        x,s,_,_,_=simulate(cfg,profiles,20,.25,81,{10:.05,30:-.05})
        self.assertEqual([sum(a['policy']==role for a in s['accounts']) for role in ['random','reversion','episode']],[40,20,20])
        self.assertEqual(x['account_gold'].shape,(31,80))
        self.assertEqual(x['intensity'][1],3)  # Zero history, 80 * original .75/20.
        self.assertGreater(x['trades'].sum(),0)
        self.assertTrue((x['account_gold']>=0).all() and (x['account_foreign']>=0).all())
        np.testing.assert_allclose(x['gold']+x['account_gold'].sum(axis=1),1400000+np.cumsum(x['injection_gold']),atol=1e-8,rtol=0)
        np.testing.assert_allclose(x['pool_foreign']+x['account_foreign'].sum(axis=1),280000+np.cumsum(x['injection_foreign']),atol=1e-8,rtol=0)
        np.testing.assert_allclose(x['account_wealth'],x['account_gold']+x['account_foreign']*x['prices'][:,None],atol=1e-8,rtol=0)
        self.assertEqual(Decimal(s['pool_gold'])+sum(Decimal(a['gold']) for a in s['accounts']),Decimal('1400000')+Decimal(s['issued_gold']))
        self.assertEqual(Decimal(s['pool_foreign'])+sum(Decimal(a['foreign']) for a in s['accounts']),Decimal('280000')+Decimal(s['issued_foreign']))


if __name__=='__main__':unittest.main()
