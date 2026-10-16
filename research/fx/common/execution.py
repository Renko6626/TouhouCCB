"""Finite-wallet execution with fresh production AMM quotes."""
from decimal import Decimal
from backend.app.services.fx.amm import quote_buy, quote_buy_exact_out, quote_sell
from backend.app.services.fx.quantize import amount_down
from .amm import Order


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
