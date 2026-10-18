import math
import unittest
from decimal import Decimal as D
import numpy as np

from research.fx.common.amm import Account,Pool
from research.fx.factor_activity.model import components,target_position,execute_target
from research.fx.factor_activity.run import load_config,simulate


class FactorActivityTests(unittest.TestCase):
    def test_signal_uses_log_mean_and_window_average_trend(self):
        cfg=dict(trend_window=2,mean_window=2,noise_sigma=.01)
        parts=components([math.log(4),math.log(5)],math.log(6),2,2,.5,cfg)
        self.assertAlmostEqual(parts[0],math.log(1.5))
        self.assertAlmostEqual(parts[1],.5*math.log(math.sqrt(20)/6))
        self.assertAlmostEqual(parts[2],.02)

    def test_budget_buy_uses_actual_fill_for_next_adjustment(self):
        cfg=dict(position_cap=500,sensitivity=100,adjustment=.2,max_pool_fraction=.005)
        pool=Pool(D('1000000'),D('200000'),D(0));account=Account('a',D(5000),D(1000))
        target=target_position(.01,cfg)
        raw,quote=execute_target(pool,account,target,D(1000),D(5),cfg)
        self.assertGreater(raw,0)
        self.assertLess(float(quote.output_amount),raw)
        self.assertEqual(pool.gold+account.gold,D(1005000))
        self.assertEqual(pool.foreign+account.foreign,D(201000))
        second,_=execute_target(pool,account,target,D(1000),pool.price,cfg)
        self.assertAlmostEqual(second,.2*(float(target)-float(quote.output_amount)))

    def test_sell_can_lower_baseline_inventory_without_borrowing(self):
        cfg=dict(position_cap=500,sensitivity=100,adjustment=.2,max_pool_fraction=.005)
        pool=Pool(D('1000000'),D('200000'),D(0));account=Account('a',D(5000),D(1000))
        wanted,quote=execute_target(pool,account,D(-500),D(1000),D(5),cfg)
        self.assertEqual(wanted,-100)
        self.assertEqual(account.foreign,D(900))
        self.assertEqual(quote.input_amount,D(100))

    def test_feedback_preserves_forecast_stream_and_uses_pre_return_state(self):
        cfg={**load_config(),'ticks':200,'burn_in':50}
        fixed,_=simulate(cfg,'factor_fixed',41)
        hybrid,_=simulate(cfg,'factor_activity',41)
        np.testing.assert_array_equal(fixed['forecast_noise_sum'],hybrid['forecast_noise_sum'])
        np.testing.assert_allclose(hybrid['intensity'][1:],.75+.25*hybrid['z_pre'][1:]**2)
        a=math.exp(-1/1000)
        np.testing.assert_allclose(hybrid['z_pre'][2:],a*hybrid['z_pre'][1:-1]+math.sqrt(1-a*a)*hybrid['returns'][1:-1]/.0004)

    def test_regular_budget_quantization_is_not_wallet_clipping(self):
        cfg={**load_config(),'ticks':100,'burn_in':20,'accounts':1,'initial_gold':1000000}
        x,_=simulate(cfg,'factor_fixed',41)
        self.assertGreater(x['trades'].sum(),0)
        self.assertEqual(x['clipped_orders'].sum(),0)
