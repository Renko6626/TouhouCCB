"""Observable spot-inventory episodes, actual costs, and real partial exits."""
from decimal import Decimal as D
import unittest

from research.fx.common.amm import Account, Order, Pool
from research.fx.episodes.model import BehaviorTrader, liquidation_mark


CFG = dict(entry_quantity=100, take_profit=.01, stop_loss=.01, trail_arm=.005,
           trail_drawdown=.005, max_pool_fraction=.005)


class EpisodeTests(unittest.TestCase):
    def test_entry_wait_and_long_exit_use_actual_cashflow(self):
        pool, account = Pool(D('1000000'),D('200000'),D('0')), Account('a',D('5000'),D('1000'))
        trader = BehaviorTrader('fixed')
        self.assertEqual(trader.arrive(pool,account,.0001,.0005,1,CFG)['action'],'wait')
        opened = trader.arrive(pool,account,.001,.0005,2,CFG)
        self.assertEqual(opened['action'],'open')
        self.assertEqual(account.foreign,D('1100'))
        episode = trader.episode
        self.assertEqual(episode.entry_gold,D('5000')-account.gold)
        before = account.gold, account.foreign
        self.assertEqual(trader.arrive(pool,account,-1,.0005,3,CFG)['action'],'wait')
        self.assertEqual((account.gold,account.foreign),before)
        pool.inject(gold=D('20000'))
        closed = trader.arrive(pool,account,-1,.0005,4,CFG)
        self.assertEqual(closed['reason'],'take_profit')
        self.assertTrue(closed['complete'])
        self.assertIsNone(trader.episode)
        self.assertEqual(account.foreign,D('1000'))
        self.assertEqual(episode.realized_profit,account.gold-D('5000'))
        self.assertGreater(episode.realized_profit,0)

    def test_sell_then_rebuy_is_owned_inventory_and_loss_is_cash_loss(self):
        pool, account = Pool(D('1000000'),D('200000'),D('0')), Account('a',D('5000'),D('1000'))
        trader = BehaviorTrader('fixed')
        trader.arrive(pool,account,-.001,.0005,1,CFG)
        episode = trader.episode
        self.assertEqual(account.foreign,D('900'))
        self.assertEqual(episode.entry_gold,account.gold-D('5000'))
        pool.inject(gold=D('100000'))
        self.assertLess(liquidation_mark(pool,episode)['return'],-.01)
        closed = trader.arrive(pool,account,1,.0005,2,CFG)
        self.assertEqual(closed['reason'],'stop_loss')
        self.assertTrue(closed['complete'])
        self.assertEqual(account.foreign,D('1000'))
        self.assertEqual(episode.realized_profit,account.gold-D('5000'))
        self.assertLess(episode.realized_profit,0)

    def test_observed_gain_then_drawdown_triggers_trailing_exit(self):
        pool, account = Pool(D('1000000'),D('200000'),D('0')), Account('a',D('5000'),D('1000'))
        trader = BehaviorTrader('trailing')
        trader.arrive(pool,account,.001,.0005,1,CFG)
        pool.inject(gold=D('8000'))
        held = trader.arrive(pool,account,0,.0005,2,CFG)
        self.assertEqual(held['action'],'wait')
        self.assertGreater(trader.episode.peak_return,.005)
        self.assertLess(trader.episode.peak_return,.01)
        pool.trade(Account('other',D('0'),D('600')),Order('sell',D('600')))
        closed = trader.arrive(pool,account,0,.0005,3,CFG)
        self.assertEqual(closed['reason'],'trailing')
        self.assertTrue(closed['complete'])
        self.assertGreater(D(closed['realized_profit']),0)

    def test_partial_rebuy_keeps_remaining_position_without_issuing_cash(self):
        pool, account = Pool(D('1000000'),D('200000'),D('0')), Account('a',D('0'),D('1000'))
        trader = BehaviorTrader('fixed')
        trader.arrive(pool,account,-.001,.0005,1,CFG)
        episode = trader.episode
        pool.inject(gold=D('100000'))
        partial = trader.arrive(pool,account,0,.0005,2,CFG)
        self.assertEqual(partial['reason'],'stop_loss')
        self.assertFalse(partial['complete'])
        self.assertIs(trader.episode,episode)
        self.assertGreater(episode.remaining,0)
        self.assertLess(episode.remaining,D('100'))
        self.assertEqual(account.foreign,D('1000')-episode.remaining)
        self.assertEqual(account.gold,D('0'))
        blocked = trader.arrive(pool,account,.001,.0005,3,CFG)
        self.assertEqual(blocked['action'],'unfilled')
        self.assertIs(trader.episode,episode)
        self.assertEqual(account.gold,D('0'))

    def test_one_trade_per_account_per_tick_prevents_instant_reentry(self):
        pool, account = Pool(D('1000000'),D('200000'),D('0')), Account('a',D('5000'),D('1000'))
        trader = BehaviorTrader('fixed')
        trader.arrive(pool,account,.001,.0005,1,CFG)
        pool.inject(gold=D('20000'))
        self.assertEqual(trader.arrive(pool,account,.001,.0005,1,CFG)['action'],'same_tick')
        self.assertEqual(trader.arrive(pool,account,.001,.0005,2,CFG)['action'],'close')
        self.assertEqual(trader.arrive(pool,account,.001,.0005,2,CFG)['action'],'same_tick')
        self.assertEqual(trader.arrive(pool,account,.001,.0005,3,CFG)['action'],'open')
