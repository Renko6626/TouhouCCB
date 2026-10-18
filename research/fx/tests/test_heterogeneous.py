import unittest
import numpy as np

from research.fx.factor_activity.run import load_config,simulate as original_simulate
from research.fx.heterogeneous.profiles import make_profiles
from research.fx.heterogeneous.run import simulate


class HeterogeneousTests(unittest.TestCase):
    def test_fixed_profiles_preserve_type_means_and_noise_variance(self):
        cfg=load_config();p=make_profiles(cfg,301,1)
        self.assertEqual(p,make_profiles(cfg,301,1))
        self.assertNotEqual(p,make_profiles(cfg,302,1))
        self.assertEqual(len(p),20)
        for group,(a,b) in enumerate(cfg['factor_mix']):
            rows=[r for r in p if r['type']==group]
            self.assertAlmostEqual(np.mean([r['trend'] for r in rows]),a)
            self.assertAlmostEqual(np.mean([r['reversion'] for r in rows]),b)
            self.assertAlmostEqual(np.mean([r['sigma']**2 for r in rows]),cfg['noise_sigma']**2,places=15)
        for key in ['trend','reversion','sigma']:
            self.assertTrue(all(r[key]>0 for r in p))
            self.assertEqual(len({r[key] for r in p}),20)

    def test_zero_spread_reproduces_real_old_simulation_and_noise_pairing(self):
        cfg={**load_config(),'ticks':180,'burn_in':30}
        profiles=make_profiles(cfg,301,0)
        for case,b in [('factor_fixed',0),('factor_activity',.25)]:
            old,_=original_simulate(cfg,case,71)
            new,_=simulate(cfg,profiles,b,71)
            for key in old:np.testing.assert_array_equal(new[key],old[key],err_msg=key)
        profiles=make_profiles(cfg,301,1)
        off,_=simulate(cfg,profiles,0,71);on,_=simulate(cfg,profiles,.25,71)
        np.testing.assert_array_equal(off['forecast_noise_sum'],on['forecast_noise_sum'])
        np.testing.assert_allclose(on['intensity'][1:],.75+.25*on['z_pre'][1:]**2)
