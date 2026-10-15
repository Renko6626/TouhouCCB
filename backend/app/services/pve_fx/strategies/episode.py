import math
from app.services.pve_fx.presets import TICK_SECONDS
from app.services.pve_fx.types import FxPreparedSignal, FxDecision, FxOrderIntent
from . import decimal, normal, state_copy


class EpisodeStrategy:
    @staticmethod
    def prepare_tick(bot, frame, rng):
        p = bot.params
        tw, mw = int(p['trend_window_seconds']) // TICK_SECONDS, int(p['mean_window_seconds']) // TICK_SECONDS
        logs = [math.log(float(price)) for price in frame.closes[-max(tw, mw):]]
        trend = (logs[-1] - logs[-tw]) / tw if len(logs) >= tw else 0.
        mean = sum(logs[-mw:]) / mw - logs[-1] if len(logs) >= mw else 0.
        noise = float(p['sigma']) * normal(rng)
        signal = float(p['trend']) * float(p['trend_multiplier']) * trend + float(p['reversion']) * mean + noise
        return FxPreparedSignal(frame.reference_price, signal, noise, {'tick_id': frame.tick_id, 'trend': trend, 'mean': mean})

    @staticmethod
    def decide(bot, prepared, execution_view):
        state = state_copy(bot)
        tick, p = prepared.values['tick_id'], bot.params
        if state.get('last_action_tick') == tick:
            return FxDecision(state=state, reason='same_tick')
        episode = state.get('episode')
        if not episode:
            if prepared.signal == 0 or abs(prepared.signal) < float(p['sigma']):
                return FxDecision(state=state, reason='wait')
            direction = 1 if prepared.signal > 0 else -1
            quantity = decimal(p['entry_quantity'])
            if direction < 0:
                quantity = min(quantity, bot.foreign_amount)
            if quantity <= 0:
                return FxDecision(state=state, reason='no_inventory')
            state['pending'] = {'tick': tick, 'direction': direction, 'opening': True}
            return FxDecision(FxOrderIntent('buy_foreign' if direction > 0 else 'sell_foreign', quantity, 'signal'), state, 'signal')
        remaining, basis = decimal(episode['remaining']), decimal(episode['basis'])
        if episode['direction'] == 1:
            pnl = execution_view.quote_sell(remaining).output_amount - basis
        else:
            pnl = basis - execution_view.quote_buy_exact_out(remaining).input_amount
        rate = float(pnl / basis)
        peak = max(rate, episode['peak_return']) if episode.get('peak_return') is not None else rate
        episode['peak_return'] = peak
        reason = episode.get('closing_reason')
        if reason is None:
            if rate >= float(p['take_profit']):
                reason = 'take_profit'
            elif rate <= -float(p['stop_loss']):
                reason = 'stop_loss'
            elif peak >= float(p['trail_arm']) and peak - rate >= float(p['trail_drawdown']):
                reason = 'trailing'
        if reason is None:
            return FxDecision(state=state, reason='wait')
        episode['closing_reason'] = reason
        state['pending'] = {'tick': tick, 'opening': False}
        return FxDecision(FxOrderIntent('sell_foreign' if episode['direction'] == 1 else 'buy_foreign', remaining, reason), state, reason)

    @staticmethod
    def on_fill(state, fill):
        from copy import deepcopy
        result = deepcopy(state)
        pending = result.pop('pending')
        quantity = abs(fill.foreign_after - fill.foreign_before)
        gold_change = fill.cash_after - fill.cash_before
        result['last_action_tick'] = pending['tick']
        if pending['opening']:
            result['episode'] = {'direction': pending['direction'], 'entry_tick': pending['tick'],
                                 'remaining': str(quantity), 'basis': str(abs(gold_change)),
                                 'realized_profit': '0', 'peak_return': None, 'closing_reason': None}
        else:
            episode = result['episode']
            remaining, basis = decimal(episode['remaining']), decimal(episode['basis'])
            if quantity > remaining:
                raise ValueError('fill exceeds episode remaining quantity')
            allocated = basis if quantity == remaining else basis * quantity / remaining
            profit = gold_change - allocated if episode['direction'] == 1 else allocated + gold_change
            episode.update(remaining=str(remaining - quantity), basis=str(basis - allocated),
                           realized_profit=str(decimal(episode['realized_profit']) + profit))
            if quantity == remaining:
                result['episode'] = None
        return result
