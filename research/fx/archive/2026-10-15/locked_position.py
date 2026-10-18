"""One entry order, unchanged actual inventory, then an opposite-signal exit."""

import math
from decimal import Decimal

from backend.app.services.fx.quantize import amount_down

from .amm import Order
from .position import PositionTrader


class LockedPositionTrader(PositionTrader):
    def __init__(self, account, *, entry_threshold=.0005, exit_threshold=-.00025, **kwargs):
        super().__init__(account, **kwargs)
        if not (math.isfinite(entry_threshold) and math.isfinite(exit_threshold)
                and entry_threshold > 0 and -entry_threshold < exit_threshold <= 0):
            raise ValueError('locked positions require positive entry and -entry < exit <= 0')
        self.entry_threshold = entry_threshold
        self.exit_threshold = exit_threshold
        self.entries = self.exits = self.completed_hold_ticks = 0
        self.entry_tick = None
        self.hold_ticks = []

    def decide(self, view, rng):
        self.update_expectation(view, rng)
        if self.rate_per_tick == 0:
            return []
        if self.next_arrival is None:
            self.next_arrival = view.tick + rng.expovariate(self.rate_per_tick)
        arrived = False
        while self.next_arrival <= view.tick:
            self.total_arrivals += 1
            # Preserve the existing per-arrival draws, including inactive arrivals.
            rng.random()
            self.next_arrival += rng.expovariate(self.rate_per_tick)
            arrived = True
        if not arrived:
            return []

        position = view.foreign - self.initial_foreign
        signal = self.expected_log_gap
        if position:
            direction = 1 if position > 0 else -1
            if direction * signal >= self.exit_threshold:
                return []
            # Sell the actual long inventory, or buy the exact missing foreign
            # quantity to close a short including fees and AMM slippage.
            order = Order('sell' if position > 0 else 'buy_exact_out', abs(position))
            self.exits += 1
            duration = view.tick - self.entry_tick
            self.completed_hold_ticks += duration
            self.hold_ticks.append(duration)
            self.entry_tick = None
            return [order]

        if abs(signal) <= self.entry_threshold:
            return []
        quantity = amount_down(abs(self.position_cap * Decimal(str(math.tanh(self.sensitivity * signal)))))
        amount = amount_down(quantity * view.price) if signal > 0 else quantity
        if amount <= Decimal('0.000001'):
            return []
        self.entries += 1
        self.entry_tick = view.tick
        return [Order('buy' if signal > 0 else 'sell', amount)]
