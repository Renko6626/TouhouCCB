"""Pure FX strategies; prepared values are ephemeral, state is JSON compatible."""
from copy import deepcopy
from decimal import Decimal


def decimal(value):
    return Decimal(str(value))


def normal(rng):
    """Support the engine's NumPy generator and standard-library Random."""
    return float(rng.normal() if hasattr(rng, 'normal') else rng.gauss(0, 1))


def state_copy(bot):
    return deepcopy(bot.strategy_state)
