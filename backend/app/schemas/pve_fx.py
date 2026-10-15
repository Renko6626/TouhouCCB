"""Validated FX bot configuration. All durations are seconds."""
from decimal import Decimal
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
