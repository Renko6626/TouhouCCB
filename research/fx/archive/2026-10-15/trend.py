"""Poisson trades with a sigmoid direction bias from window returns."""

import math
from decimal import Decimal

from .amm import Account
from .bots import MarketView, sigmoid
from .noise import NoiseTrader


class TrendTrader(NoiseTrader):
    def __init__(self, account: Account, *, lookback_ticks: int = 30,
                 sensitivity: float = 100, rate_per_tick: float = 0.05,
                 gold_size: Decimal = Decimal("200")):
        super().__init__(account, rate_per_tick=rate_per_tick, gold_size=gold_size)
        if not isinstance(lookback_ticks, int) or lookback_ticks <= 0:
            raise ValueError("lookback_ticks must be a positive integer")
        if not math.isfinite(sensitivity) or sensitivity < 0:
            raise ValueError("sensitivity must be finite and nonnegative")
        self.lookback_ticks = lookback_ticks
        self.sensitivity = sensitivity

    def buy_probability(self, view: MarketView) -> float:
        if len(view.history) < self.lookback_ticks:
            return 0.5
        past_price = view.history[-self.lookback_ticks].price
        window_return = float(view.price / past_price - 1)
        return sigmoid(self.sensitivity * window_return)
