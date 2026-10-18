"""Committed pricing inputs, serialized refresh and process-local coverage stamps."""
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4
from typing import Sequence
from fastapi import HTTPException
from sqlalchemy import select
from app.core.database import async_session_maker
from app.models.base import Market, Outcome
from app.models.fx import FxPair
from app.services.market_read_state import FxReadState, LmsrReadState, PricingRecord, PricingStamp
from app.services.credit.gates import GATES
from app.services.credit.keys import GroupKey as GateKey
from app.services.credit.ownership import OWNERSHIP
from app.services.credit import flags
from app.schemas.trading_context import GroupKey, GroupStamp, FxPricingGroup, LmsrPricingGroup

def decimal_string(value):
    return format(Decimal(str(value)), 'f')

def clock_string(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat() if value.tzinfo is None else value.astimezone(timezone.utc).isoformat()

def now_string():
    return datetime.now(timezone.utc).isoformat()

def input_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

class PricingRegistry:
    """One committed projection; memory reuse requires covered writes and ownership."""

    def __init__(self, session_factory=None):
        self.session_factory = session_factory or async_session_maker
        self.memory_products = frozenset()
        self.epoch = str(uuid4())
        self._locks = {}
        self._entries = {}
        self._hashes = {}
        self._revisions = {}

    def configure(self, *, memory_products: frozenset[str]) -> None:
        self.memory_products = frozenset(memory_products)

    def reset(self):
        """Reset only when readers have stopped; a new epoch invalidates coverage."""
        self.epoch = str(uuid4())
        self._locks.clear()
        self._entries.clear()
        self._hashes.clear()
        self._revisions.clear()

    def _can_reuse(self, product):
        return (product in self.memory_products and OWNERSHIP.is_owner
                and OWNERSHIP.writes_enabled and not flags.read_only_from_env())

    @staticmethod
    def _pricing_data(state):
        if isinstance(state, FxReadState):
            return dict(product='fx', id=state.id,
                        gold_reserve=decimal_string(state.gold_reserve),
                        foreign_reserve=decimal_string(state.foreign_reserve),
                        buy_fee_rate=decimal_string(state.buy_fee_rate),
                        sell_fee_rate=decimal_string(state.sell_fee_rate),
                        pool_version=decimal_string(state.pool_version),
                        status=state.status, archived=state.archived,
                        reduce_only=state.reduce_only)
        return dict(product='lmsr', id=state.id, b=decimal_string(state.b),
                    status=state.status, closes_at=clock_string(state.closes_at),
                    outcome_ids=list(state.outcome_ids),
                    q_dec=[decimal_string(q) for q in state.q_dec])

    def _stamp(self, key, digest):
        if self._hashes.get(key) != digest:
            self._revisions[key] = self._revisions.get(key, 0) + 1
        self._hashes[key] = digest
        return PricingStamp(self.epoch, str(self._revisions[key]), digest)

    def install_committed(self, state: FxReadState | LmsrReadState) -> None:
        """No IO/await: existing-market writers call before releasing their GATE.

        Startup prewarming and post-commit installation of a newly created key
        are the explicit exceptions to the caller-held write GATE requirement.
        """
        key = (state.product, state.id)
        digest = input_hash(self._pricing_data(state))
        self._entries[key] = PricingRecord(state, self._stamp(key, digest), now_string())

    def invalidate(self, key: GroupKey) -> None:
        # Unavailable is not a database-confirmed deletion: retain the last hash.
        self._entries.pop((key.product, key.id), None)

    def peek_fx(self, pair_id: int) -> FxReadState | None:
        """Synchronous locked-row callers must also compare pool_version themselves."""
        if not self._can_reuse('fx'):
            return None
        record = self._entries.get(('fx', pair_id))
        return record.state if record is not None else None

    async def _snapshot(self, db, key):
        if key.product == 'fx':
            pair = (await db.execute(select(FxPair).where(FxPair.id == key.id))).scalar_one_or_none()
            return FxReadState.from_pair(pair) if pair is not None else None
        rows = (await db.execute(select(Market.liquidity_b, Market.status, Market.closes_at, Outcome.id, Outcome.total_shares).outerjoin(Outcome, Outcome.market_id == Market.id).where(Market.id == key.id).order_by(Outcome.id))).all()
        if not rows or rows[0][3] is None:
            return None
        return LmsrReadState(id=key.id, b=rows[0][0], status=rows[0][1],
                             closes_at=rows[0][2], outcome_ids=tuple(r[3] for r in rows),
                             q_dec=tuple(r[4] for r in rows))

    async def _record_locked(self, key):
        """Caller holds the batch GATE; only then take a per-key loading lock."""
        identity = (key.product, key.id)
        lock = self._locks.setdefault(identity, asyncio.Lock())
        async with lock:
            previous = self._entries.get(identity)
            if self._can_reuse(key.product) and previous is not None:
                return previous
            try:
                async with self.session_factory() as db:
                    state = await self._snapshot(db, key)
            except Exception:
                self.invalidate(key)
                raise
            if state is None:
                # A creation commits/installs without a key GATE. Its new snapshot
                # must survive an older cold SELECT that observed no row.
                current = self._entries.get(identity)
                if current is not previous and current is not None:
                    return current if self._can_reuse(key.product) else None
                self._entries.pop(identity, None)
                self._stamp(identity, input_hash(None))
                return None
            self.install_committed(state)
            return self._entries[identity]

    @staticmethod
    def _keys(keys):
        return [GroupKey(product=p, id=i) for p, i in sorted({(k.product, k.id) for k in keys})]

    async def _records_locked(self, keys):
        return [await self._record_locked(key) for key in keys]

    async def _read_records(self, keys, *, require_coverage):
        keys = self._keys(keys)
        if not require_coverage:
            records = [self._entries.get((key.product, key.id))
                       if self._can_reuse(key.product) else None for key in keys]
            if all(record is not None for record in records):
                return records
        # No session is opened while waiting. Declare the entire batch once.
        async with GATES.hold(shared=[GateKey(k.product, k.id) for k in keys]):
            return await self._records_locked(keys)

    def _groups(self, records):
        groups = []
        for record in records:
            if record is None:
                continue
            data = self._pricing_data(record.state)
            cls = FxPricingGroup if record.state.product == 'fx' else LmsrPricingGroup
            groups.append(cls(**data, as_of=record.observed_at,
                              stamp=GroupStamp(epoch=record.stamp.epoch,
                                               revision=record.stamp.revision,
                                               input_hash=record.stamp.input_hash)))
        return groups

    async def read_groups(self, keys: Sequence[GroupKey], *, require_coverage: bool = True) -> list[FxPricingGroup | LmsrPricingGroup]:
        return self._groups(await self._read_records(keys, require_coverage=require_coverage))

    async def read_groups_locked(self, keys: Sequence[GroupKey]) -> list[FxPricingGroup | LmsrPricingGroup]:
        """Internal integration helper: caller already holds every requested GATE."""
        keys = self._keys(keys)
        if not {GateKey(k.product, k.id) for k in keys}.issubset(GATES.held_keys_by_current_task()):
            raise RuntimeError('read_groups_locked requires all market GATES')
        return self._groups(await self._records_locked(keys))

    async def read_fx(self, pair_id: int) -> FxReadState:
        records = await self._read_records([GroupKey(product='fx', id=pair_id)], require_coverage=False)
        if records[0] is None:
            raise HTTPException(status_code=404, detail='FX pair not found')
        return records[0].state

    async def refresh_groups(self, keys):
        """Compatibility name for covered reads of committed market state."""
        return await self.read_groups(keys)

    async def warm_fx(self) -> None:
        """Startup only, before economic producers; uncovered products stay in DB mode."""
        if not self._can_reuse('fx'):
            return
        async with self.session_factory() as db:
            pairs = (await db.execute(select(FxPair))).scalars().all()
            states = [FxReadState.from_pair(pair) for pair in pairs]
        for state in states:
            self.install_committed(state)

REGISTRY = PricingRegistry()

async def refresh_groups(keys):
    return await REGISTRY.refresh_groups(keys)

# One shared committed projection read per active group, independent of subscriber count.
class PricingPublisher:
    def __init__(self):
        self.subscriptions = {}
        self.task = None
        self.wake = asyncio.Event()

    def subscribe(self, subscription, keys):
        self.subscriptions[subscription] = {(key.product, key.id) for key in keys}

    def unsubscribe(self, subscription):
        self.subscriptions.pop(subscription, None)

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self.run())

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        self.subscriptions.clear()

    async def run(self):
        import logging
        previous = {}
        while True:
            try:
                try:
                    await asyncio.wait_for(self.wake.wait(), 1)
                except asyncio.TimeoutError:
                    pass
                self.wake.clear()
                keys = set().union(*self.subscriptions.values()) if self.subscriptions else set()
                # Public pricing does not claim coverage of a just-read account.
                groups = await REGISTRY.read_groups([GroupKey(product=p, id=i) for p, i in keys], require_coverage=False)
                by_key = {(g.product, g.id): g for g in groups}
                changed = {key for key in keys if previous.get(key) != (by_key[key].stamp.input_hash if key in by_key else None)}
                # Serialize each changed group once; filtering only builds a
                # subscriber-specific list of these read-only dictionaries.
                dumped = {key: by_key[key].model_dump() for key in changed if key in by_key}
                for subscription, wanted in tuple(self.subscriptions.items()):
                    affected = changed & wanted
                    if affected:
                        subscription.offer('pricing', dict(groups=[dumped[k] for k in sorted(affected) if k in dumped], missing_groups=[dict(product=k[0], id=k[1]) for k in sorted(affected) if k not in by_key], server_time=now_string()))
                previous = {key: by_key[key].stamp.input_hash if key in by_key else None for key in keys}
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).exception('Trading pricing refresh failed')

PUBLISHER = PricingPublisher()

def notify_pricing_changed(keys):
    """Nonthrowing commit hint; periodic read also repairs lost final hints."""
    try:
        PUBLISHER.wake.set()
    except Exception:
        pass
