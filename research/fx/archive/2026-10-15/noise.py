"""Independent Poisson arrivals and equally likely buy/sell directions."""

import math
from decimal import Decimal
from random import Random

from backend.app.services.fx.quantize import amount_down

from .amm import Account, Order, nonnegative
from .bots import Bot, MarketView


class NoiseTrader(Bot):
    def __init__(self, account: Account, *, rate_per_tick: float = 0.05,
                 gold_size: Decimal = Decimal("200")):
        super().__init__(account)
        if not math.isfinite(rate_per_tick) or rate_per_tick < 0:
            raise ValueError("rate_per_tick must be finite and nonnegative")
        nonnegative(gold_size)
        if gold_size == 0:
            raise ValueError("gold_size must be positive")
        self.rate_per_tick = rate_per_tick
        self.gold_size = gold_size
        self.next_arrival: float | None = None
        self.total_arrivals = 0

    def buy_probability(self, view: MarketView) -> float:
        return 0.5

    def decide(self, view: MarketView, rng: Random) -> list[Order]:
        if self.rate_per_tick == 0:
            return []
        if self.next_arrival is None:
            self.next_arrival = view.tick + rng.expovariate(self.rate_per_tick)
        orders = []
        # Collect every arrival in the interval ending at this tick, not just
        # the first one. Fractional arrival times are binned to integer ticks.
        while self.next_arrival <= view.tick:
            self.total_arrivals += 1
            if rng.random() < self.buy_probability(view):
                orders.append(Order("buy", self.gold_size))
            else:
                orders.append(Order("sell", amount_down(self.gold_size / view.price)))
            self.next_arrival += rng.expovariate(self.rate_per_tick)
        return orders
