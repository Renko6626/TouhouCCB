"""Personal threshold rules and finite-wallet execution; no shared sentiment."""
from decimal import Decimal
import math
import random
import numpy as np

from backend.app.services.fx.amm import quote_buy, quote_buy_exact_out, quote_sell
from backend.app.services.fx.quantize import amount_down
from .amm import Account, Order


def hysteresis(state, signal, on, off):
    if signal > on:
        return 1
    if signal < -on:
        return -1
    if state == 1 and signal < off or state == -1 and signal > -off:
        return 0
    return state


def probability(difference, beta):
    x = beta * difference
    if x >= 0:
        return 1 / (1 + math.exp(-x))
    ex = math.exp(x)
    return ex / (1 + ex)


def execute(pool, account, wanted, max_fraction):
    """Requote and clip at execution time. Return None for untradeable dust."""
    quantity = amount_down(min(Decimal(str(abs(wanted))),
                               pool.foreign * Decimal(str(max_fraction))))
    if wanted < 0:
        quantity = min(quantity, account.foreign)
    if quantity <= 0 or wanted == 0 or wanted > 0 and account.gold <= 0:
        return None
    try:
        if wanted > 0:
            quote = quote_buy_exact_out(quantity, pool.gold, pool.foreign, pool.fee_rate)
            order = Order('buy_exact_out', quantity)
            if quote.input_amount > account.gold:
                quote = quote_buy(account.gold, pool.gold, pool.foreign, pool.fee_rate)
                order = Order('buy', account.gold)
        else:
            quote_sell(quantity, pool.gold, pool.foreign, pool.fee_rate)
            order = Order('sell', quantity)
    except ValueError as exc:
        if str(exc) in ('output rounds to zero', 'fee leaves no input'):
            return None
        raise
    return pool.trade(account, order)


class Agent:
    def __init__(self, index, kind, adaptive, seed, cfg, initial_price):
        self.kind, self.adaptive, self.cfg = kind, adaptive, cfg
        self.amplitude = cfg['position_cap']
        self.base = 2 * self.amplitude
        self.account = Account(str(index), amount_down(Decimal(str(self.base * initial_price))),
                               Decimal(str(self.base)))
        self.ema = math.log(initial_price)
        self.gamma = cfg['score_gammas'][index % len(cfg['score_gammas'])]
        self.scores = np.zeros(2)
        self.shadow_z = [0, 0]
        self.shadow_u = np.zeros(2)
        self.signals = [0., 0.]
        self.state = 0
        self.switches = self.opportunities = 0
        self.choice_rng = random.Random(seed * 1000003 + index * 101 + 3)

    def score(self, realized_return, held_exposure):
        """Mark this interval using a snapshot taken before any interval trades."""
        self.scores *= 1 - self.gamma
        self.scores += self.gamma * held_exposure * realized_return

    def observe(self, log_price, history):
        alpha = 2 / (self.cfg['mean_window'] + 1)
        self.ema += alpha * (log_price - self.ema)
        past = history[max(0, len(history) - 1 - self.cfg['trend_window'])]
        self.signals = [self.cfg['trend_strength'] * (log_price - past),
                        self.cfg['reversion_strength'] * (self.ema - log_price)]

    def reconsider(self, mode, beta):
        if not self.adaptive or mode not in ('random', 'performance'):
            return
        if self.choice_rng.random() >= self.cfg['choice_rate']:
            return
        self.opportunities += 1
        p = probability(self.scores[0] - self.scores[1], beta if mode == 'performance' else 0)
        kind = 'M' if self.choice_rng.random() < p else 'R'
        self.switches += kind != self.kind
        self.kind = kind

    def intent_gap(self):
        """Prospective next-wake demand; observing it must not activate a state."""
        state = hysteresis(self.state, self.signals[self.kind == 'R'],
                           self.cfg['entry_threshold'], self.cfg['exit_threshold'])
        return self.base + self.amplitude * state - float(self.account.foreign)

    def wake(self):
        on, off = self.cfg['entry_threshold'], self.cfg['exit_threshold']
        for s in range(2):
            self.shadow_z[s] = hysteresis(self.shadow_z[s], self.signals[s], on, off)
            self.shadow_u[s] += self.cfg['adjustment'] * (self.shadow_z[s] - self.shadow_u[s])
        self.state = hysteresis(self.state, self.signals[self.kind == 'R'], on, off)
        target = self.base + self.amplitude * self.state
        return self.cfg['adjustment'] * (target - float(self.account.foreign))
