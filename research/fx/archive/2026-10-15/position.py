"""Tanh target foreign inventory; orders move actual holdings toward the target."""

import math
from decimal import Decimal

from backend.app.services.fx.quantize import amount_down

from .amm import Order, nonnegative
from .expectation import ExpectationTrader


class PositionTrader(ExpectationTrader):
    def __init__(self, account, *, position_cap=Decimal('500'), adjustment=.2, **kwargs):
        super().__init__(account, **kwargs)
        nonnegative(position_cap)
        if position_cap == 0 or position_cap > account.foreign:
            raise ValueError('positive position cap needs sufficient backing foreign inventory')
        if not math.isfinite(adjustment) or not 0 < adjustment <= 1:
            raise ValueError('adjustment must be in (0,1]')
        self.position_cap = position_cap
        self.adjustment = Decimal(str(adjustment))
        self.initial_foreign = account.foreign
        self.target_position = Decimal(0)

    @property
    def position(self):
        return self.account.foreign - self.initial_foreign

    def update_target(self):
        self.target_position = self.position_cap * Decimal(str(math.tanh(self.sensitivity*self.expected_log_gap)))

    def on_arrival(self, tick):
        pass

    def decide(self, view, rng):
        self.update_expectation(view, rng)
        self.update_target()
        if self.rate_per_tick == 0:
            return []
        if self.next_arrival is None:
            self.next_arrival = view.tick + rng.expovariate(self.rate_per_tick)
        planned = view.foreign - self.initial_foreign
        orders = []
        while self.next_arrival <= view.tick:
            self.total_arrivals += 1
            # Reserve the old direction draw to keep arrival streams paired.
            # Trade direction itself is deterministic given target and holdings.
            rng.random()
            self.on_arrival(view.tick)
            gap = self.target_position-planned
            quantity = amount_down(abs(self.adjustment*gap))
            if quantity > Decimal('0.000001'):
                if gap > 0:
                    gold = amount_down(quantity*view.price)
                    if gold > Decimal('0.000001'):
                        orders.append(Order('buy',gold))
                        planned += quantity
                else:
                    orders.append(Order('sell',quantity))
                    planned -= quantity
            self.next_arrival += rng.expovariate(self.rate_per_tick)
        # Within a batch every order has one direction. Buying by q*observed P
        # fills <=q because fees/slippage reduce output and later buys raise P.
        # planned is therefore an upper bound on buy fills, preventing overshoot.
        # On the next tick actual account.foreign is read again, not this estimate.
        return orders
