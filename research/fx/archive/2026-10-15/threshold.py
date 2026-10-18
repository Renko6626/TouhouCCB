"""F–J-inspired hysteresis targets; existing AMM fills and Poisson scheduling.

Uses our endogenous three-factor forecast, not F–J's external value process.
State changes only at an arrival; execution still approaches target gradually.
"""
import math
from decimal import Decimal

from .position import PositionTrader


class ThresholdTrader(PositionTrader):
    def __init__(self, account, *, entry_threshold=.001, exit_threshold=0., **kwargs):
        super().__init__(account, **kwargs)
        if not (math.isfinite(entry_threshold) and math.isfinite(exit_threshold)
                and entry_threshold > 0 and -entry_threshold < exit_threshold < entry_threshold):
            raise ValueError('thresholds require positive entry and -entry < exit < entry')
        self.entry_threshold = entry_threshold
        self.exit_threshold = exit_threshold
        self.state = 0
        self.entries = self.exits = self.completed_hold_ticks = 0
        self.entry_tick = None

    def update_target(self):
        # A new forecast does not itself erase an existing holding target.
        pass

    def on_arrival(self, tick):
        signal = self.expected_log_gap
        if self.state == 0:
            if signal > self.entry_threshold:
                self.state = 1
            elif signal < -self.entry_threshold:
                self.state = -1
            if self.state:
                self.entries += 1
                self.entry_tick = tick
        elif (self.state == 1 and signal < self.exit_threshold
              or self.state == -1 and signal > -self.exit_threshold):
            self.state = 0
            self.exits += 1
            self.completed_hold_ticks += tick-self.entry_tick
            self.entry_tick = None
        # Exit goes to flat; a later arrival is required for a new entry.
        self.target_position = self.position_cap * Decimal(self.state)
