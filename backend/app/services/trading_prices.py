"""Committed pricing inputs, serialized refresh and process-local coverage stamps."""
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import async_session_maker
from app.models.base import Market, Outcome
from app.models.fx import FxPair
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
    def __init__(self, session_factory=None):
        self.session_factory = session_factory or async_session_maker
        self.epoch = str(uuid4())
        self._locks = {}
        self._entries = {}
        self._revisions = {}

    def reset(self):
        """Reset only when readers have stopped; a new epoch invalidates coverage."""
        self.epoch = str(uuid4())
        self._locks.clear()
        self._entries.clear()
        self._revisions.clear()

    async def _snapshot(self, db, key):
        if key.product == 'fx':
            columns = ('gold_reserve', 'foreign_reserve', 'buy_fee_rate', 'sell_fee_rate', 'pool_version', 'status', 'archived', 'reduce_only')
            row = (await db.execute(select(*(getattr(FxPair, c) for c in columns)).where(FxPair.id == key.id))).first()
            if row is None:
                return None
            values = dict(zip(columns, row))
            for name in columns[:5]:
                values[name] = decimal_string(values[name])
            return dict(product='fx', id=key.id, **values)
        rows = (await db.execute(select(Market.liquidity_b, Market.status, Market.closes_at, Outcome.id, Outcome.total_shares).outerjoin(Outcome, Outcome.market_id == Market.id).where(Market.id == key.id).order_by(Outcome.id))).all()
        if not rows or rows[0][3] is None:
            return None
        return dict(product='lmsr', id=key.id, b=decimal_string(rows[0][0]), status=rows[0][1], closes_at=clock_string(rows[0][2]), outcome_ids=[r[3] for r in rows], q_dec=[decimal_string(r[4]) for r in rows])

    async def refresh_groups(self, keys):
        result = []
        for key in sorted({(k.product, k.id) for k in keys}):
            lock = self._locks.setdefault(key, asyncio.Lock())
            async with lock:
                async with self.session_factory() as db:
                    data = await self._snapshot(db, GroupKey(product=key[0], id=key[1]))
                digest = input_hash(data)
                previous = self._entries.get(key)
                if previous is None or previous[0] != digest:
                    self._revisions[key] = self._revisions.get(key, 0) + 1
                self._entries[key] = (digest, data)
                if data is not None:
                    cls = FxPricingGroup if key[0] == 'fx' else LmsrPricingGroup
                    result.append(cls(**data, as_of=now_string(), stamp=GroupStamp(epoch=self.epoch, revision=str(self._revisions[key]), input_hash=digest)))
        return result

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
                # Registry installation is serialized with context and supplementation.
                groups = await REGISTRY.refresh_groups([GroupKey(product=p, id=i) for p, i in keys])
                by_key = {(g.product, g.id): g for g in groups}
                changed = {key for key in keys if previous.get(key) != (by_key[key].stamp.input_hash if key in by_key else None)}
                for subscription, wanted in tuple(self.subscriptions.items()):
                    affected = changed & wanted
                    if affected:
                        subscription.offer('pricing', dict(groups=[by_key[k].model_dump() for k in sorted(affected) if k in by_key], missing_groups=[dict(product=k[0], id=k[1]) for k in sorted(affected) if k not in by_key], server_time=now_string()))
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
