import unittest

import numpy as np

from research.fx.reversion.repeated.timing_metrics import event_response,price_ranges


class PhysicalTimeMetricsTests(unittest.TestCase):
    def test_five_second_ticks_use_full_thirty_minutes(self):
        prices=np.r_[np.linspace(100,102,361),np.linspace(102,104,361)[1:]]
        result=price_ranges(prices,5)
        self.assertEqual(result['windows'],2)
        self.assertAlmostEqual(result['median'],(.02+2/102)/2)

    def test_ten_minute_response_is_120_ticks_at_five_seconds(self):
        prices=np.full(122,105.);prices[0]=100.;prices[110:]=102.5
        event=dict(tick=1,pre_price=100,post_price=105,return_requested=.05)
        five=event_response(prices,event,121,5)
        ten=event_response(prices,event,121,10)
        self.assertEqual(five['retraced_10m'],.5)
        self.assertEqual(ten['retraced_10m'],0)
        self.assertAlmostEqual(five['first_half_minutes'],109*5/60)
        terminal=event_response(prices,event,1,5)
        self.assertIsNone(terminal['retraced_10m'])
        self.assertIsNone(terminal['first_half_minutes'])


if __name__=='__main__':unittest.main()
