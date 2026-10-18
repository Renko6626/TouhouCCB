"""Bounded inventory under real AMM fills and batched Poisson arrivals."""

import unittest
from decimal import Decimal as D
from random import Random

from research.fx.amm import Account, Pool
from research.fx.bots import MarketView
from research.fx.position import PositionTrader
from research.fx.threshold import ThresholdTrader
from research.fx.locked_position import LockedPositionTrader


class FixedSignalLocked(LockedPositionTrader):
    def update_expectation(self, view, rng):
        self.expected_log_gap = self.signal


class FixedSignalThreshold(ThresholdTrader):
    def update_expectation(self, view, rng):
        self.expected_log_gap = self.signal


class FixedSignalTrader(PositionTrader):
    def update_expectation(self, view, rng):
        self.expected_log_gap = self.signal


class PositionTests(unittest.TestCase):
    def test_locked_inventory_stays_put_until_strong_opposite_signal(self):
        for direction in (1,-1):
            with self.subTest(direction=direction):
                account=Account('locked',D('1e12'),D('2e11'))
                pool=Pool(D('1000000'),D('200000'))
                bot=FixedSignalLocked(account,position_cap=D('100'),rate_per_tick=.000001,
                                      entry_threshold=.001,exit_threshold=-.0005)
                rng=Random(3)
                def act(tick,signal):
                    bot.signal=signal;bot.next_arrival=tick
                    orders=bot.decide(MarketView(tick,pool.price,(),account.gold,account.foreign,tick=tick),rng)
                    for order in orders:
                        pool.trade(account,order)
                    return orders
                self.assertEqual(len(act(0,direction*.002)),1)
                opened=bot.position
                self.assertGreater(direction*opened,D('19'))
                self.assertLess(abs(opened),D('100'))
                for tick,signal in enumerate((direction*.01,direction*.0001,0,-direction*.0002),1):
                    self.assertEqual(act(tick,signal),[])
                    self.assertEqual(bot.position,opened)
                self.assertEqual(len(act(5,-direction*.002)),1)
                self.assertEqual(bot.position,D(0))
                self.assertEqual((bot.entries,bot.exits),(1,1))
                self.assertEqual(bot.completed_hold_ticks,5)

    def test_locked_multiple_arrivals_do_not_duplicate_or_reverse_in_same_tick(self):
        account=Account('locked',D('1e12'),D('2e11'))
        pool=Pool(D('1000000'),D('200000'))
        bot=FixedSignalLocked(account,position_cap=D('100'),rate_per_tick=100,
                              entry_threshold=.001,exit_threshold=-.0005)
        rng=Random(3)
        for tick,signal in ((1,.002),(2,-.002)):
            bot.signal=signal;bot.next_arrival=tick-1
            orders=bot.decide(MarketView(tick,pool.price,(),account.gold,account.foreign,tick=tick),rng)
            self.assertEqual(len(orders),1)
            for order in orders:
                pool.trade(account,order)
        self.assertGreater(bot.total_arrivals,100)
        self.assertEqual(bot.position,D(0))
        self.assertEqual((bot.entries,bot.exits),(1,1))

    def test_threshold_holds_weak_signal_and_exits_before_reversing(self):
        account=Account('threshold',D('1e12'),D('2e11'))
        bot=FixedSignalThreshold(account,position_cap=D('100'),adjustment=1,
                                 rate_per_tick=.000001,entry_threshold=.001)
        pool=Pool(D('1000000'),D('200000')); rng=Random(3)
        for tick,(signal,target) in enumerate(((.002,100),(.0002,100),(-.0001,0),(-.002,-100))):
            bot.signal=signal;bot.next_arrival=tick
            orders=bot.decide(MarketView(tick,pool.price,(),account.gold,account.foreign,tick=tick),rng)
            self.assertEqual(bot.target_position,D(target))
            for order in orders:
                pool.trade(account,order)
                self.assertLessEqual(abs(bot.position),D('100'))
        self.assertEqual(bot.entries,2)
        self.assertEqual(bot.exits,1)
        self.assertLess(bot.position,D('-99'))

    def exercise(self, signal):
        account=Account('bounded',D('1e12'),D('2e11'))
        bot=FixedSignalTrader(account,position_cap=D('100'),adjustment=1,
                              rate_per_tick=10)
        bot.signal=signal
        pool=Pool(D('1000000'),D('200000'))
        rng=Random(3); max_position=D(0); total=D(0)
        for tick in range(50):
            orders=bot.decide(MarketView(tick,pool.price,(),account.gold,account.foreign,tick=tick),rng)
            for order in orders:
                quote=pool.trade(account,order)
                total+=quote.output_amount if order.side=='buy' else quote.input_amount
                max_position=max(max_position,abs(bot.position))
        return bot,pool,max_position,total

    def test_sustained_signal_saturates_instead_of_trading_forever(self):
        for signal in (1,-1):
            bot,pool,maximum,total=self.exercise(signal)
            self.assertLessEqual(maximum,D('100'))
            self.assertLessEqual(total,D('100'))
            self.assertGreater(abs(bot.position),D('99.99'))
            self.assertGreater(bot.total_arrivals,100)

    def test_round_trips_keep_inventory_bounded_but_gross_volume_grows(self):
        account=Account('bounded',D('1e12'),D('2e11'))
        bot=FixedSignalTrader(account,position_cap=D('100'),adjustment=1,rate_per_tick=10)
        pool=Pool(D('1000000'),D('200000'));rng=Random(3);volume=D(0)
        for tick in range(30):
            bot.signal=1 if tick%2 else -1
            orders=bot.decide(MarketView(tick,pool.price,(),account.gold,account.foreign,tick=tick),rng)
            for order in orders:
                quote=pool.trade(account,order)
                volume+=quote.output_amount if order.side=='buy' else quote.input_amount
                self.assertLessEqual(abs(bot.position),D('100'))
        self.assertGreater(volume,D('1000'))


if __name__=='__main__':
    unittest.main()
