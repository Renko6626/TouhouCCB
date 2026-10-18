"""Tick-based bot interface and immutable observations."""

from abc import ABC, abstractmethod
import math
from dataclasses import dataclass, field
from functools import cached_property
from decimal import Decimal
from random import Random

from .amm import Account, Order


def sigmoid(value: float) -> float:
    if value >= 0:
        return 1 / (1 + math.exp(-value))
    exp_value = math.exp(value)
    return exp_value / (1 + exp_value)


@dataclass(frozen=True)
class PricePoint:
    time_seconds: int
    price: Decimal

    @cached_property
    def log_price(self) -> float:
        return math.log(self.price)


@dataclass(frozen=True)
class MarketView:
    time_seconds: int
    price: Decimal
    history: tuple[PricePoint, ...]
    gold: Decimal
    foreign: Decimal
    tick: int = 0
    _mean_log_cache: dict[int, float] = field(default_factory=dict, repr=False, compare=False)

    def mean_log_price(self, window: int) -> float:
        if window not in self._mean_log_cache:
            self._mean_log_cache[window] = sum(p.log_price for p in self.history[-window:]) / window
        return self._mean_log_cache[window]


class Bot(ABC):
    def __init__(self, account: Account):
        self.account = account

    @abstractmethod
    def decide(self, view: MarketView, rng: Random) -> Order | list[Order] | None:
        """Update once per tick; return zero, one or multiple orders.

        The view contains past sampled prices and current balances, never
        scheduled future injections. Store strategy memory on the bot itself.
        """
