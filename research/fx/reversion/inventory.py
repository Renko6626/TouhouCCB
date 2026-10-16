"""Existing budget-based target adjustment, with actual fills recorded."""
from decimal import Decimal

from backend.app.services.fx.quantize import amount_down
from research.fx.factor_activity.model import target_position,execute_target


def inventory_step(pool,account,gap,baseline,observed_price,cfg):
    target_cfg={**cfg,'position_cap':cfg['inventory_position_cap'],
                'sensitivity':cfg['inventory_sensitivity'],'adjustment':cfg['inventory_adjustment']}
    target=target_position(gap,target_cfg)
    wanted=Decimal(str(cfg['inventory_adjustment']))*(target-(account.foreign-baseline))
    quantity=amount_down(abs(wanted));limited=amount_down(min(quantity,pool.foreign*Decimal(str(cfg['max_pool_fraction']))))
    clipped=limited<quantity
    if wanted<0:clipped=clipped or account.foreign<limited
    else:clipped=clipped or account.gold<amount_down(limited*observed_price)
    before_gold,before_foreign=account.gold,account.foreign
    pool_before=dict(gold=str(pool.gold),foreign=str(pool.foreign))
    _,quote=execute_target(pool,account,target,baseline,observed_price,target_cfg)
    return dict(wanted=float(wanted),target=str(target),gap=gap,filled=quote is not None,
                clipped=clipped or quote is None and quantity>0,
                actual_flow=float(account.foreign-before_foreign),gold_change=str(account.gold-before_gold),
                before_gold=str(before_gold),before_foreign=str(before_foreign),
                after_gold=str(account.gold),after_foreign=str(account.foreign),pool_before=pool_before,
                pool_after=dict(gold=str(pool.gold),foreign=str(pool.foreign)))
