"""Throwaway policy adapters around the frozen simulator, without editing it.

An isolated import of entry_model gets two local callbacks. Its components
callback observes each completed close before any current-tick events; its
inventory callback changes only accounts 10--14. No global simulator is patched.
"""
from collections import deque
from decimal import Decimal
import importlib.util
import math
from pathlib import Path

import numpy as np

from research.fx.factor_activity.model import execute_target


PRESETS = [
    dict(name='fan_bull', anchor_offset=.015, herd=.03, swing=.1, trend=.6, shock=.02),
    dict(name='fan_bear', anchor_offset=-.015, herd=.03, swing=.1, trend=.6, shock=.02),
    dict(name='chaser', anchor_offset=.003, herd=.3, swing=.7, trend=1.2, shock=.06),
    dict(name='swinger', anchor_offset=-.003, herd=.1, swing=.8, trend=.7, shock=.06),
    dict(name='bottom_fisher', anchor_offset=0., herd=.05, swing=.75, trend=-1., shock=.06),
]
POLICY = dict(window=360, mad_multiplier=2., minimum_scale=.001,
              belief_scale=.01, belief_threshold=.001, anchor_revert=.05,
              shock_scale=.002)


def isolated_simulator():
    path = Path(__file__).parents[1] / 'reversion/repeated/entry_model.py'
    spec = importlib.util.spec_from_file_location(
        'research.fx.reversion.repeated._lmsr_exploration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Policies:
    def __init__(self, variant, seed):
        self.variant = variant
        self.history = deque(maxlen=POLICY['window'])
        self.tick = 0
        self.states = {}
        self.rngs = {i: np.random.default_rng(np.random.SeedSequence([seed, i, 20261016]))
                     for i in range(10, 15)}
        self.records = []

    def observe(self, original):
        def wrapped(past, current, *args):
            self.tick += 1
            self.history.append(current)
            return original(past, current, *args)
        return wrapped

    def step(self, pool, account, gap, baseline, observed_price, cfg):
        history = np.asarray(self.history)
        current = history[-1]
        extra = {}
        if self.variant == 'robust_inventory':
            center = float(np.median(history))
            mad = float(np.median(abs(history - center)))
            scale = max(1.4826 * mad, POLICY['minimum_scale'])
            deviation = current - center
            excess = max(0., abs(deviation) - POLICY['mad_multiplier'] * scale)
            score = -math.copysign(excess, deviation)
            target = cfg['inventory_position_cap'] * math.tanh(score / scale)
            extra = dict(center=center, mad=mad, scale=scale, score=score)
        else:
            i = int(account.name)
            p = PRESETS[i - 10]
            rng = self.rngs[i]
            state = self.states.setdefault(i, dict(
                anchor=math.log(cfg['pool_gold'] / cfg['pool_foreign']) + p['anchor_offset'],
                belief=math.log(cfg['pool_gold'] / cfg['pool_foreign']) + p['anchor_offset']))
            if rng.random() < p['shock']:
                shock = float(rng.uniform(-1, 1)) * POLICY['shock_scale']
                state['anchor'] += shock
                state['belief'] += shock
            state['belief'] += (p['herd'] * (current - state['belief'])
                                + POLICY['anchor_revert'] * (state['anchor'] - state['belief']))
            trend = current - history[-cfg['trend_window']] if len(history) >= cfg['trend_window'] else 0.
            score = ((1 - p['swing']) * (state['belief'] - current)
                     + p['swing'] * p['trend'] * trend)
            target = cfg['inventory_position_cap'] * math.tanh(score / POLICY['belief_scale'])
            extra = dict(persona=p['name'], score=score, **state)
            # Same no-signal semantics as the existing target-inventory model:
            # neutral targets return toward initial inventory, rather than freeze it.
            if abs(score) < POLICY['belief_threshold']:
                target = 0.
        target = Decimal(str(target))
        target_cfg = {**cfg, 'adjustment': cfg['inventory_adjustment']}
        before_gold, before_foreign = account.gold, account.foreign
        wanted, quote = execute_target(pool, account, target, baseline, observed_price, target_cfg)
        actual = float(account.foreign - before_foreign)
        record = dict(tick=self.tick, observed_through_tick=self.tick - 1,
                      account=int(account.name), wanted=wanted, target=str(target),
                      gap=gap, filled=quote is not None,
                      clipped=quote is None and abs(wanted) > 1e-6 or abs(actual) + 2e-6 < abs(wanted),
                      actual_flow=actual, gold_change=str(account.gold - before_gold),
                      before_gold=str(before_gold), before_foreign=str(before_foreign),
                      after_gold=str(account.gold), after_foreign=str(account.foreign), **extra)
        self.records.append(record)
        return {key: value for key, value in record.items() if key not in ('tick', 'account')}


def simulate(variant, cfg, frozen, seed, schedule):
    module = isolated_simulator()
    adapter = None
    if variant != 'baseline':
        adapter = Policies(variant, seed)
        module.components = adapter.observe(module.components)
        module.inventory_step = adapter.step
    result = module.simulate(cfg, frozen['profiles'], 5, frozen['activity_b'], seed,
                             schedule, frozen['policy'],
                             dict(trend_multiplier=frozen['trend_multiplier']))
    return result, [] if adapter is None else adapter.records
