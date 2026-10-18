"""Immutable committed market data; never retain ORM objects or sessions."""
from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from decimal import Decimal
from typing import ClassVar, Literal


@dataclass(frozen=True)
class FxReadState:
    id: int
    gold_reserve: Decimal
    foreign_reserve: Decimal
    buy_fee_rate: Decimal
    sell_fee_rate: Decimal
    pool_version: int
    status: str
    archived: bool
    reduce_only: bool
    currency_code: str
    currency_name: str
    created_at: datetime
    updated_at: datetime
    product: ClassVar[Literal['fx']] = 'fx'

    @classmethod
    def from_pair(cls, pair) -> FxReadState:
        """Copy loaded database values, after any required flush/refresh."""
        return cls(**{field.name: getattr(pair, field.name) for field in fields(cls)})


@dataclass(frozen=True)
class LmsrReadState:
    id: int
    b: float
    outcome_ids: tuple[int, ...]
    q_dec: tuple[Decimal, ...]
    status: str
    closes_at: datetime | None
    product: ClassVar[Literal['lmsr']] = 'lmsr'

    def __post_init__(self) -> None:
        # Writer callers may supply lists; no mutable collection crosses the boundary.
        object.__setattr__(self, 'outcome_ids', tuple(self.outcome_ids))
        object.__setattr__(self, 'q_dec', tuple(Decimal(str(q)) for q in self.q_dec))


@dataclass(frozen=True)
class PricingStamp:
    epoch: str
    revision: str
    input_hash: str


@dataclass(frozen=True)
class PricingRecord:
    state: FxReadState | LmsrReadState
    stamp: PricingStamp
    observed_at: str
