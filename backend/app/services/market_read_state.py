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


# FX-only transaction descriptors. Keep ORM references local to their owner
# transaction; the Registry only receives the immutable copies above.
def _sync_session(session):
    return getattr(session, 'sync_session', getattr(session, '_session', session))


def stage_fx_pair(session, pair) -> None:
    owner = _sync_session(session)
    transaction = owner.get_transaction()
    if transaction is None:
        raise RuntimeError('FX changes require an active transaction')
    owner.info.setdefault('fx_read_changes', {})[int(pair.id)] = (transaction, pair)


def capture_fx_changes(session) -> tuple[FxReadState, ...]:
    """After flush, copy only rows registered by this outer transaction."""
    owner = _sync_session(session)
    transaction = owner.get_transaction()
    changes = owner.info.pop('fx_read_changes', {})
    return tuple(FxReadState.from_pair(pair) for tx, pair in changes.values()
                 if tx is transaction)


def invalidate_fx_changes(changes) -> None:
    from app.schemas.trading_context import GroupKey
    from app.services.trading_prices import REGISTRY
    for state in changes:
        REGISTRY.invalidate(GroupKey(product='fx', id=state.id))


def install_fx_changes(changes: tuple[FxReadState, ...]) -> None:
    """Committed DB state must never leave an older entry available on failure."""
    from app.services.trading_prices import REGISTRY
    try:
        for state in changes:
            REGISTRY.install_committed(state)
    except Exception:
        import logging
        invalidate_fx_changes(changes)
        logging.getLogger(__name__).exception('FX database committed; snapshot installation failed')


async def commit_fx_changes(session) -> None:
    """Existing FX commit owners retain their locks and notification ordering."""
    await session.flush()
    changes = capture_fx_changes(session)
    try:
        await session.commit()
    except BaseException:
        # An interrupted acknowledgement may follow a durable commit.
        invalidate_fx_changes(changes)
        raise
    install_fx_changes(changes)
