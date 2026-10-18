"""Mixed trend, log-price mean reversion and independent forecast noise."""

import math
from decimal import Decimal
from random import Random

from .amm import Account, Order
from .bots import MarketView, sigmoid
from .noise import NoiseTrader


class ExpectationTrader(NoiseTrader):
    def __init__(self, account: Account, *, trend_window: int = 30,
                 mean_window: int = 30, trend_strength: float = 30,
                 reversion_strength: float = 0.1, noise_sigma: float = 0.005,
                 sensitivity: float = 100, rate_per_tick: float = 0.05,
                 gold_size: Decimal = Decimal("200")):
        super().__init__(account, rate_per_tick=rate_per_tick, gold_size=gold_size)
        for window in (trend_window, mean_window):
            if not isinstance(window, int) or window <= 0:
                raise ValueError("windows must be positive integers")
        for value in (trend_strength, reversion_strength, noise_sigma, sensitivity):
            if not math.isfinite(value) or value < 0:
                raise ValueError("signal parameters must be finite and nonnegative")
        self.trend_window = trend_window
        self.mean_window = mean_window
        self.trend_strength = trend_strength
        self.reversion_strength = reversion_strength
        self.noise_sigma = noise_sigma
        self.sensitivity = sensitivity
        self.expected_log_gap = 0.0
        self.target_price: Decimal | None = None
        self.trend_component = 0.0
        self.reversion_component = 0.0
        self.noise_component = 0.0

    def decide(self, view: MarketView, rng: Random) -> list[Order]:
        self.update_expectation(view, rng)
        return super().decide(view, rng)

    def update_expectation(self, view: MarketView, rng: Random) -> None:
        current_log = math.log(view.price)
        trend = mean_reversion = 0.0
        if len(view.history) >= self.trend_window:
            past_log = view.history[-self.trend_window].log_price
            trend = (current_log - past_log) / self.trend_window
        if len(view.history) >= self.mean_window:
            mean_log = view.mean_log_price(self.mean_window)
            mean_reversion = mean_log - current_log
        self.trend_component = self.trend_strength * trend
        self.reversion_component = self.reversion_strength * mean_reversion
        self.noise_component = self.noise_sigma * rng.gauss(0, 1)
        self.expected_log_gap = (self.trend_component + self.reversion_component
                                 + self.noise_component)
        self.target_price = view.price * Decimal(str(math.exp(self.expected_log_gap)))

    def buy_probability(self, view: MarketView) -> float:
        return sigmoid(self.sensitivity * self.expected_log_gap)
