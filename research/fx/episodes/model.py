"""One self-funded buy/sell or inventory-sale/repurchase episode at a time."""
from dataclasses import dataclass, asdict
from decimal import Decimal

from backend.app.services.fx.amm import quote_buy_exact_out, quote_sell
from research.fx.common.execution import execute


@dataclass
class Episode:
    id: str
    account: str
    direction: int
    entry_tick: int
    entry_quantity: Decimal
    entry_gold: Decimal
    remaining: Decimal
    basis: Decimal
    realized_profit: Decimal = Decimal('0')
    peak_return: float | None = None
    closing_reason: str | None = None
    close_tick: int | None = None

    def record(self):
        return {k: str(v) if isinstance(v,Decimal) else v for k,v in asdict(self).items()}


def liquidation_mark(pool, episode):
    """Full remaining-quantity production quote, before wallet affordability clipping."""
    if episode.direction == 1:
        quote = quote_sell(episode.remaining,pool.gold,pool.foreign,pool.fee_rate)
        pnl = quote.output_amount-episode.basis
        amount = quote.output_amount
    else:
        quote = quote_buy_exact_out(episode.remaining,pool.gold,pool.foreign,pool.fee_rate)
        pnl = episode.basis-quote.input_amount
        amount = quote.input_amount
    return {'gold':amount,'profit':pnl,'return':float(pnl/episode.basis)}


@dataclass
class BehaviorTrader:
    variant: str
    episode: Episode | None = None
    sequence: int = 0
    last_action_tick: int = -1

    def __post_init__(self):
        if self.variant not in ('fixed','trailing'):
            raise ValueError('unknown episode variant')

    def arrive(self,pool,account,signal,sigma,tick,cfg):
        if self.last_action_tick == tick:
            return dict(action='same_tick')
        mark = None
        if self.episode is None:
            if signal == 0 or abs(signal) < sigma:
                return dict(action='wait')
            direction = 1 if signal > 0 else -1
            wanted = direction*cfg['entry_quantity']
            action,reason = 'open','signal'
        else:
            episode = self.episode
            mark = liquidation_mark(pool,episode)
            rate = mark['return']
            episode.peak_return = rate if episode.peak_return is None else max(episode.peak_return,rate)
            reason = episode.closing_reason
            if reason is None:
                if rate >= cfg['take_profit']:
                    reason = 'take_profit'
                elif rate <= -cfg['stop_loss']:
                    reason = 'stop_loss'
                elif (self.variant == 'trailing' and episode.peak_return >= cfg['trail_arm']
                      and episode.peak_return-rate >= cfg['trail_drawdown']):
                    reason = 'trailing'
            if reason is None:
                return dict(action='wait',mark_return=rate)
            episode.closing_reason = reason
            direction = episode.direction
            wanted = -direction*float(episode.remaining)
            action = 'close'
        before_gold,before_foreign = account.gold,account.foreign
        pool_before = dict(gold=str(pool.gold),foreign=str(pool.foreign))
        quote = execute(pool,account,wanted,cfg['max_pool_fraction'])
        if quote is None:
            return dict(action='unfilled',intention=action,reason=reason,wanted=float(wanted))
        actual = account.foreign-before_foreign
        gold_change = account.gold-before_gold
        quantity = abs(actual)
        self.last_action_tick = tick
        profit,allocated = Decimal('0'),Decimal('0')
        if action == 'open':
            self.sequence += 1
            basis = abs(gold_change)
            episode = Episode(f'{account.name}:{self.sequence}',account.name,direction,tick,
                              quantity,basis,quantity,basis)
            self.episode = episode
        else:
            episode = self.episode
            if quantity > episode.remaining:
                raise AssertionError('closed more than episode quantity')
            allocated = episode.basis if quantity == episode.remaining else episode.basis*quantity/episode.remaining
            profit = gold_change-allocated if direction == 1 else allocated+gold_change
            episode.realized_profit += profit
            episode.remaining -= quantity
            episode.basis -= allocated
            if episode.remaining == 0:
                episode.close_tick = tick
                self.episode = None
        return dict(action=action,reason=reason,episode_id=episode.id,direction=direction,tick=tick,
                    wanted=float(wanted),actual_flow=float(actual),quantity=str(quantity),
                    gold_change=str(gold_change),fee=str(quote.fee_amount),allocated_basis=str(allocated),
                    realized_profit=str(profit),remaining=str(episode.remaining),basis=str(episode.basis),
                    complete=episode.close_tick is not None,mark_return=None if mark is None else mark['return'],
                    peak_return=episode.peak_return,clipped=abs(float(actual))+2e-6 < abs(wanted),
                    before_gold=str(before_gold),before_foreign=str(before_foreign),
                    after_gold=str(account.gold),after_foreign=str(account.foreign),
                    pool_before=pool_before,pool_after=dict(gold=str(pool.gold),foreign=str(pool.foreign)))
