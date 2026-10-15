import math
from app.services.fx.quantize import amount_down
from app.services.pve_fx.presets import TICK_SECONDS
from app.services.pve_fx.types import FxPreparedSignal, FxDecision, FxOrderIntent
from . import decimal, state_copy


class InventoryStrategy:
    @staticmethod
    def prepare_tick(bot, frame, rng):
        width = int(bot.params['inventory_window_seconds']) // TICK_SECONDS
        logs = [math.log(float(p)) for p in frame.closes[-width:]]
        gap = sum(logs) / len(logs) - logs[-1] if logs else 0.
        target = decimal(bot.params['inventory_position_cap']) * decimal(
            math.tanh(float(bot.params['inventory_sensitivity']) * gap))
        return FxPreparedSignal(frame.reference_price, values={'target': target, 'tick_id': frame.tick_id})

    @staticmethod
    def decide(bot, prepared, execution_view):
        state = state_copy(bot)
        tick = prepared.values['tick_id']
        if state.get('last_action_tick') == tick:
            return FxDecision(state=state, reason='same_tick')
        wanted = decimal(bot.params['inventory_adjustment']) * (
            prepared.values['target'] - (bot.foreign_amount - decimal(bot.params['inventory_baseline_foreign'])))
        if not wanted:
            return FxDecision(state=state, reason='at_target')
        # Budget semantics use the tick's fixed observed price, after pool clipping.
        quantity = amount_down(min(abs(wanted), execution_view.foreign_reserve * decimal(prepared.values.get('max_pool_fraction', '.005'))))
        intent = FxOrderIntent('buy_gold', amount_down(quantity * prepared.reference_price), 'inventory') if wanted > 0 else FxOrderIntent('sell_foreign', quantity, 'inventory')
        state['pending_tick'] = tick
        return FxDecision(intent, state, 'inventory')

    @staticmethod
    def on_fill(state, fill):
        result = dict(state)
        result['last_action_tick'] = result.pop('pending_tick')
        return result
