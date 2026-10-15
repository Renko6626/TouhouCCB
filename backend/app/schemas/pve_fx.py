"""Validated FX bot configuration. All durations are seconds."""
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, PositiveInt


class FxPairBotConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    enabled: bool = False
    activity_b: float = Field(0.25, ge=0, le=1)
    tau_seconds: float = Field(2700, gt=0)
    mean_arrivals_per_20: float = Field(1, ge=0)
    psi: float = Field(0.0008, gt=0)
    max_pool_fraction: Decimal = Field(Decimal("0.005"), gt=0, le=Decimal("0.005"))
    death_floor_gold: Decimal = Field(Decimal("3"), ge=0)


class FxPveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    pair_configs: dict[PositiveInt, FxPairBotConfig] = Field(default_factory=dict)


FX_AMOUNT_MAX = Decimal('9999999999.999999')


class FxPveGenerateItem(BaseModel):
    model_config = ConfigDict(extra='forbid')
    template: Literal['random', 'inventory', 'episode']
    count: int = Field(ge=1, le=50)


class FxPveGenerateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    pair_id: PositiveInt
    items: list[FxPveGenerateItem] = Field(min_length=1)
    initial_cash: Decimal = Field(ge=0, le=100000, decimal_places=6)
    initial_foreign: Decimal = Field(ge=0, le=FX_AMOUNT_MAX, decimal_places=6)
    naming_style: Literal['npc', 'lowkey', 'phrase'] = 'lowkey'


class FxPvePatchRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: Literal['active', 'paused'] | None = None
    template: Literal['random', 'inventory', 'episode'] | None = None
    params: dict | None = None


class FxPveFundRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    amount: Decimal = Field(gt=0, le=100000, decimal_places=6)
    reason: str | None = Field(default=None, max_length=200)


class FxPveBotItem(BaseModel):
    profile_id: int
    user_id: int
    username: str
    pair_id: int
    template: str
    params: dict
    status: str
    strategy_state: dict
    inventory_baseline_foreign: Decimal
    cash: Decimal
    wallet: Decimal
    mtm: Decimal
    liquidation_cash: Decimal
