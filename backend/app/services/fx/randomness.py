"""Deterministic random walk helpers for the FX engine."""
from __future__ import annotations

import math
import random
from decimal import Decimal
from typing import Any


class FxRandomSource:
    @staticmethod
    def step_target(log_target: Decimal, elapsed_sec: float, hourly_sigma: Decimal, rng: Any = None) -> Decimal:
        """Advance a log target using Brownian noise scaled by elapsed hours.

        ``rng`` may be a ``random.Random`` instance or any object exposing
        ``gauss(mu, sigma)``; accepting an injected source keeps ticks replayable.
        """
        if elapsed_sec <= 0 or hourly_sigma <= 0:
            return Decimal(log_target)
        source = rng or random
        shock = source.gauss(0.0, float(hourly_sigma) * math.sqrt(float(elapsed_sec) / 3600.0))
        return Decimal(log_target) + Decimal(str(shock))

