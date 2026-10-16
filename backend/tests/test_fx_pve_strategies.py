from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D
import json
import math

import pytest
from app.services.pve_fx.registry import get_strategy, validate_strategy_params
from app.services.pve_fx.types import FxBotSnapshot, FxTickFrame, FxExecutionView, FxFill, FxPreparedSignal


class Noise:
    def __init__(self, values):
        self.values = iter(values)
        self.calls = 0

    def normal(self):
        self.calls += 1
        return next(self.values)


def bot(template, **params):
    return FxBotSnapshot(1, 2, 3, template, validate_strategy_params(template, params), D('5000'), D('600'))


def frame(closes=(D('1'),), tick=1):
    return FxTickFrame(tick, 3, datetime.now(timezone.utc), closes, closes[-1])


def view(price='1'):
    return FxExecutionView(D('1000000') * D(price), D('1000000'), D('.003'), D('.003'))


def fill(action, cash_before, cash_after, foreign_before, foreign_after):
    return FxFill(1, action, 'gold' if action == 'buy_foreign' else 'foreign',
                  'foreign' if action == 'buy_foreign' else 'gold', D('1'), D('1'), D('0'),
                  D(cash_before), D(cash_after), D(foreign_before), D(foreign_after), datetime.now(timezone.utc))


def test_random_draws_for_each_arrival_and_short_noise_draws_without_arrival():
    random = get_strategy('random')
    b = bot('random', order_sigma_foreign=120)
    rng = Noise([1, -2])
    prepared = random.prepare_tick(b, frame(), rng)
    assert rng.calls == 0
    assert random.decide(b, prepared, view()).intent.amount == D('120')
    assert random.decide(b, prepared, view()).intent.amount == D('240')
    episode = get_strategy('episode')
    rng = Noise([1, 2])
    episode.prepare_tick(bot('episode'), frame(), rng)
    episode.prepare_tick(bot('episode'), frame(tick=2), rng)
    assert rng.calls == 2


def test_inventory_uses_actual_initial_baseline_and_budget_semantics():
    strategy = get_strategy('inventory')
    b = bot('inventory', inventory_baseline_foreign='600', inventory_adjustment=.5, inventory_window_seconds=150)
    b = replace(b, foreign_amount=D('500'))
    prepared = strategy.prepare_tick(b, frame((D('2'),)), None)
    decision = strategy.decide(b, prepared, view())
    assert decision.intent.action == 'buy_gold'
    assert decision.intent.amount == D('100')  # half of 100 missing foreign, at observed 2
    state = strategy.on_fill(decision.state, fill('buy_foreign', '5000', '4900', '500', '549'))
    assert strategy.decide(replace(b, strategy_state=state), prepared, view()).intent is None


def test_short_history_boundary_retains_frozen_divisor_and_mean():
    strategy, b = get_strategy('episode'), bot('episode', trend_window_seconds=150, mean_window_seconds=150)
    closes = tuple(D(str(math.exp(i / 1000))) for i in range(30))
    cold = strategy.prepare_tick(b, frame(closes[:29]), Noise([0]))
    ready = strategy.prepare_tick(b, frame(closes), Noise([0]))
    assert cold.values['trend'] == cold.values['mean'] == 0
    assert ready.values['trend'] == pytest.approx(.029 / 30)
    assert ready.values['mean'] == pytest.approx(-.0145)


@pytest.mark.parametrize('direction,price', [(1, '1.1'), (-1, '.9')])
def test_episode_exit_uses_full_executable_cash_and_partial_actual_fill(direction, price):
    strategy, b = get_strategy('episode'), bot('episode')
    state = {'episode': {'direction': direction, 'remaining': '200', 'basis': '200',
                         'realized_profit': '0', 'peak_return': None, 'closing_reason': None}}
    b = replace(b, strategy_state=state)
    decision = strategy.decide(b, FxPreparedSignal(D('1'), values={'tick_id': 2}), view(price))
    assert decision.intent.reason == 'take_profit'
    assert decision.intent.amount == D('200')
    if direction == 1:
        actual = fill('sell_foreign', '5000', '5054', '600', '550')
        expected_profit = D('4')
    else:
        actual = fill('buy_foreign', '5000', '4954', '600', '650')
        expected_profit = D('4')
    after = strategy.on_fill(decision.state, actual)
    assert D(after['episode']['remaining']) == 150
    assert D(after['episode']['basis']) == 150
    assert D(after['episode']['realized_profit']) == expected_profit
    json.dumps(after)
    # A reversal cannot unlock the already selected reason, and retry is only remaining quantity.
    retry = strategy.decide(replace(b, strategy_state=after), FxPreparedSignal(D('1'), values={'tick_id': 3}), view())
    assert retry.intent.reason == 'take_profit'
    assert retry.intent.amount == D('150')
    assert state['episode']['closing_reason'] is None


def test_negative_episode_never_requests_borrowed_inventory_and_open_uses_actual_fill():
    strategy, b = get_strategy('episode'), bot('episode')
    b = replace(b, foreign_amount=D('12'))
    decision = strategy.decide(b, FxPreparedSignal(D('1'), signal=-1, values={'tick_id': 1}), view())
    assert decision.intent.action == 'sell_foreign'
    assert decision.intent.amount == D('12')
    after = strategy.on_fill(decision.state, fill('sell_foreign', '5000', '5009', '12', '2'))
    assert after['episode']['remaining'] == '10'
    assert after['episode']['basis'] == '9'


def test_ema_inventory_target_uses_supplied_log_reference():
    strategy = get_strategy('inventory')
    b = bot('inventory', inventory_baseline_foreign='600', inventory_position_cap=100,
            inventory_sensitivity=1, inventory_adjustment=.5)
    observed = replace(frame(), signals={'inventory_log_reference': math.log(2)})
    prepared = strategy.prepare_tick(b, observed, None)
    # tanh(log(2)) = 3/5, so 100 foreign of cap gives a target of 60.
    assert float(prepared.values['target']) == pytest.approx(60)
    assert b.params['inventory_ema_half_life_seconds'] == 600
