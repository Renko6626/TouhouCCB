from app.services.pve_fx.types import FxPreparedSignal, FxDecision, FxOrderIntent
from . import decimal, normal, state_copy


class RandomStrategy:
    @staticmethod
    def prepare_tick(bot, frame, rng):
        return FxPreparedSignal(frame.reference_price, values={'rng': rng})

    @staticmethod
    def decide(bot, prepared, execution_view):
        wanted = decimal(normal(prepared.values['rng'])) * decimal(bot.params['order_sigma_foreign'])
        intent = None if not wanted else FxOrderIntent(
            'buy_foreign' if wanted > 0 else 'sell_foreign', abs(wanted), 'random')
        return FxDecision(intent, state_copy(bot), 'random')

    @staticmethod
    def on_fill(state, fill):
        return dict(state)
