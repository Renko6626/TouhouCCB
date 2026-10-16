"""Validated public strategy catalogue; no database or wallet access."""
from decimal import Decimal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .presets import RANDOM_DEFAULTS, INVENTORY_DEFAULTS, SHORT_EPISODE_DEFAULTS, SHORT_EPISODE_PROFILES
from .strategies.random import RandomStrategy
from .strategies.inventory import InventoryStrategy
from .strategies.episode import EpisodeStrategy


class StrategyParams(BaseModel):
    inventory_baseline_foreign: Decimal = Field(default=Decimal('0'), ge=0, json_schema_extra={'unit': '外币', 'readOnly': True})
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)

    @field_validator('*', mode='after')
    @classmethod
    def finite(cls, value):
        if isinstance(value, Decimal) and not value.is_finite():
            raise ValueError('must be finite')
        return value


class RandomParams(StrategyParams):
    order_sigma_foreign: float = Field(default=RANDOM_DEFAULTS['order_sigma_foreign'], gt=0, json_schema_extra={'unit': '外币'})


class InventoryParams(StrategyParams):
    inventory_baseline_foreign: Decimal = Field(ge=0, json_schema_extra={'unit': '外币', 'readOnly': True})
    inventory_ema_half_life_seconds: int | None = Field(default=600, ge=5, multiple_of=5, json_schema_extra={'unit': '秒'})
    inventory_window_seconds: int | None = Field(default=None, ge=5, multiple_of=5, json_schema_extra={'unit': '秒'})

    inventory_position_cap: float = Field(default=INVENTORY_DEFAULTS['inventory_position_cap'], gt=0, json_schema_extra={'unit': '外币'})
    inventory_sensitivity: float = Field(default=INVENTORY_DEFAULTS['inventory_sensitivity'], gt=0)
    inventory_adjustment: float = Field(default=INVENTORY_DEFAULTS['inventory_adjustment'], gt=0, le=1)


    @model_validator(mode='before')
    @classmethod
    def legacy_reference(cls, values):
        # Stored old bots and the activity preset retain their original rolling mean.
        if 'inventory_window_seconds' in values and 'inventory_ema_half_life_seconds' not in values:
            values = {**values, 'inventory_ema_half_life_seconds': None}
        return values

    @model_validator(mode='after')
    def require_reference(self):
        if self.inventory_ema_half_life_seconds is None and self.inventory_window_seconds is None:
            raise ValueError('EMA半衰期和旧版滚动窗口至少填写一项')
        return self


class EpisodeParams(StrategyParams):
    trend_window_seconds: int = Field(default=SHORT_EPISODE_DEFAULTS['trend_window_seconds'], ge=5, multiple_of=5, json_schema_extra={'unit': '秒'})
    mean_window_seconds: int = Field(default=SHORT_EPISODE_DEFAULTS['mean_window_seconds'], ge=5, multiple_of=5, json_schema_extra={'unit': '秒'})
    trend_multiplier: float = Field(default=SHORT_EPISODE_DEFAULTS['trend_multiplier'], ge=0)
    entry_quantity: Decimal = Field(default=Decimal(str(SHORT_EPISODE_DEFAULTS['entry_quantity'])), gt=0, json_schema_extra={'unit': '外币'})
    take_profit: float = Field(default=SHORT_EPISODE_DEFAULTS['take_profit'], gt=0)
    stop_loss: float = Field(default=SHORT_EPISODE_DEFAULTS['stop_loss'], gt=0)
    trail_arm: float = Field(default=SHORT_EPISODE_DEFAULTS['trail_arm'], gt=0)
    trail_drawdown: float = Field(default=SHORT_EPISODE_DEFAULTS['trail_drawdown'], gt=0)
    trend: float = Field(default=SHORT_EPISODE_PROFILES[0]['trend'], ge=0)
    reversion: float = Field(default=SHORT_EPISODE_PROFILES[0]['reversion'], ge=0)
    sigma: float = Field(default=SHORT_EPISODE_PROFILES[0]['sigma'], gt=0)


FX_STRATEGY_REGISTRY = {
    'random': {'strategy': RandomStrategy, 'params_model': RandomParams, 'description': '每次到达独立随机买卖'},
    'inventory': {'strategy': InventoryStrategy, 'params_model': InventoryParams, 'description': '围绕初始外币库存逐步调节'},
    'episode': {'strategy': EpisodeStrategy, 'params_model': EpisodeParams, 'description': '按信号开仓并按可执行收益平仓'},
}


def get_strategy(template: str):
    try:
        return FX_STRATEGY_REGISTRY[template]['strategy']
    except KeyError:
        raise ValueError(f'unknown FX strategy: {template}') from None


def validate_strategy_params(template: str, params: dict) -> dict:
    get_strategy(template)
    # JSON dump makes the result safe to persist, including Decimal baselines.
    return FX_STRATEGY_REGISTRY[template]['params_model'].model_validate(params).model_dump(mode='json')


def strategy_metadata() -> list[dict]:
    return [{'template': name, 'description': entry['description'],
             'params_schema': entry['params_model'].model_json_schema(),
             'editable_when': 'paused_without_episode' if name == 'episode' else 'next_tick',
             'template_change_when': 'paused_without_episode'}
            for name, entry in FX_STRATEGY_REGISTRY.items()]
