"""Per-tick arrival, mark and execution streams inherited from the FX studies."""
import random
import numpy as np


def market_streams(seed, accounts):
    """Separate streams so mark/execution draws do not consume arrival draws."""
    arrivals=[np.random.default_rng(seed*1000003+i*101+1) for i in range(accounts)]
    marks=[np.random.default_rng(seed*1000003+i*101+2) for i in range(accounts)]
    execution=random.Random(seed*1000003+4)
    return arrivals,marks,execution


def draw_events(arrival_streams, rate_per_account, execution_rng):
    """One tick; common rate is known before draws, and repeated arrivals count."""
    events=[i for i,rng in enumerate(arrival_streams) for _ in range(rng.poisson(rate_per_account))]
    execution_rng.shuffle(events)
    return events
