"""Archived three-factor forecast and budget-based partial inventory adjustment."""
import math
from decimal import Decimal

from backend.app.services.fx.quantize import amount_down
from research.fx.common.amm import Order


def components(past_logs, current_log, epsilon, trend_strength, reversion_strength, cfg):
    """past_logs includes current observed close; mean excludes future prices."""
    tw,mw=cfg['trend_window'],cfg['mean_window']
    trend=(current_log-past_logs[-tw])/tw if len(past_logs)>=tw else 0.
    mean=sum(past_logs[-mw:])/mw-current_log if len(past_logs)>=mw else 0.
    return trend_strength*trend,reversion_strength*mean,cfg['noise_sigma']*epsilon


def target_position(gap,cfg):
    return Decimal(str(cfg['position_cap']))*Decimal(str(math.tanh(cfg['sensitivity']*gap)))


def execute_target(pool,account,target,initial_foreign,observed_price,cfg):
    """Return signed raw foreign demand and an actual production quote, or None.

    Keep the old buy budget q*observed_price; fresh pool math determines output.
    Re-evaluate from actual inventory on each event, including repeat arrivals.
    """
    wanted=Decimal(str(cfg['adjustment']))*(target-(account.foreign-initial_foreign))
    quantity=amount_down(min(abs(wanted),pool.foreign*Decimal(str(cfg['max_pool_fraction']))))
    if wanted<0:
        quantity=min(quantity,account.foreign)
        order=Order('sell',quantity)
    else:
        budget=min(amount_down(quantity*observed_price),account.gold)
        order=Order('buy',budget)
    if quantity<=Decimal('0.000001') or order.amount<=Decimal('0.000001'):
        return float(wanted),None
    try:
        return float(wanted),pool.trade(account,order)
    except ValueError as exc:
        if str(exc) in ('output rounds to zero','fee leaves no input'):
            return float(wanted),None
        raise
