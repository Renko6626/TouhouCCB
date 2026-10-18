import math
import unittest
from decimal import Decimal
import numpy as np
from research.fx.activity.model import ActivityState, load_config
from research.fx.activity.run import simulate


class ActivityTests(unittest.TestCase):
    def test_rate_is_decided_before_return_and_disabled_feedback_stays_constant(self):
        model=ActivityState(b=.25,tau=1000,mean_arrivals=1,psi=.0004)
        before=model.intensity
        model.advance(.0004)
        self.assertEqual(before,.75)
        self.assertAlmostEqual(model.intensity,.75+.25*(1-math.exp(-.002)))
        fixed=ActivityState(b=0,tau=1000,mean_arrivals=1,psi=.0004,z=20)
        fixed.advance(-.01)
        self.assertEqual(fixed.intensity,1)

    def test_conditional_moments_of_compound_gaussian_marks(self):
        model=ActivityState(b=.25,tau=1/math.log(2),mean_arrivals=1,psi=1,z=2)
        second,fourth=model.conditional_z_moments()
        self.assertAlmostEqual(second,2.3125)
        self.assertAlmostEqual(fourth,16.99609375)

    def test_full_trace_uses_pre_trade_state(self):
        cfg={**load_config(),'ticks':300,'burn_in':20}
        x,_=simulate(cfg,'linear',.25,41)
        a=math.exp(-1/1000)
        np.testing.assert_allclose(x['z_pre'][2:],a*x['z_pre'][1:-1]+math.sqrt(1-a*a)*x['returns'][1:-1]/.0004)
        np.testing.assert_allclose(x['intensity'][1:],.75+.25*x['z_pre'][1:]**2)
        np.testing.assert_allclose(x['returns'][1:],.0004*x['raw_mark_sum'][1:])
        self.assertEqual(x['z_pre'][1],0)

    def test_fixed_rate_inputs_match_across_executors_and_amm_conserves_assets(self):
        cfg={**load_config(),'ticks':500,'burn_in':50}
        linear,_=simulate(cfg,'linear',0,42)
        amm,summary=simulate(cfg,'amm',0,42)
        for field in ['arrivals','raw_mark_sum','raw_demand']:
            np.testing.assert_array_equal(linear[field],amm[field])
        self.assertEqual(Decimal(summary['pool_gold'])+sum(Decimal(a['gold']) for a in summary['accounts']),Decimal('1100000'))
        self.assertEqual(Decimal(summary['pool_foreign'])+sum(Decimal(a['foreign']) for a in summary['accounts']),Decimal('220000'))
