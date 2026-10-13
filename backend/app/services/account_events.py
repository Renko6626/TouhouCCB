"""Ephemeral private hints. Delivery failure never participates in money commits."""
import asyncio
import logging
from dataclasses import dataclass, field
from uuid import uuid4
from app.services.trading_prices import now_string
from app.schemas.fx import FxOrderResult

logger = logging.getLogger(__name__)

@dataclass(eq=False)
class Subscription:
    user_id: int
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=64))
    closed: asyncio.Event = field(default_factory=asyncio.Event)

    def offer(self, event, payload):
        if self.closed.is_set():
            return
        try:
            self.queue.put_nowait((event, payload))
        except asyncio.QueueFull:
            self.closed.set()

class AccountEvents:
    def __init__(self):
        self._subscribers = {}

    def subscribe(self, user_id):
        subscription = Subscription(user_id)
        self._subscribers.setdefault(user_id, set()).add(subscription)
        return subscription

    def unsubscribe(self, subscription):
        subscription.closed.set()
        subscribers = self._subscribers.get(subscription.user_id)
        if subscribers:
            subscribers.discard(subscription)
            if not subscribers:
                self._subscribers.pop(subscription.user_id, None)

    def close(self):
        for subscribers in list(self._subscribers.values()):
            for subscriber in list(subscribers):
                self.unsubscribe(subscriber)

    def enqueue(self, user_id, economic_version, reason, operation_ref=None):
        payload = dict(event_id=str(uuid4()), economic_version=str(economic_version), reason=reason, occurred_at=now_string())
        if operation_ref is not None:
            payload['operation_ref'] = str(operation_ref)
        for subscriber in tuple(self._subscribers.get(user_id, ())):
            subscriber.offer('account_changed', payload)

EVENTS = AccountEvents()

def enqueue_account_change(user_id, economic_version, reason, operation_ref=None):
    try:
        EVENTS.enqueue(user_id, economic_version, reason, operation_ref)
    except Exception:
        logger.exception('Private trading hint dropped')


def capture_accounts(session):
    """Materialize primitive hints while rows are live, before owner commit."""
    try:
        from sqlalchemy import inspect
        transaction = session.sync_session.get_transaction()
        changes = session.info.pop('trading_account_changes', {})
        # Rollback expires/restores ORM state; no lazy SQL here.
        return sorted({(uid, version) for uid, entries in changes.items() for owner, row, version in entries
                       if owner is transaction and inspect(row).dict.get('economic_version') == version})
    except Exception:
        logger.exception('Private trading descriptor dropped')
        return []

def publish_accounts(changes, reason):
    try:
        for user_id, version in changes:
            enqueue_account_change(user_id, version, reason)
        from app.services.trading_prices import notify_pricing_changed
        notify_pricing_changed(())
    except Exception:
        logger.exception("Committed trading hints dropped")

from contextlib import asynccontextmanager

@asynccontextmanager
async def notifying_transaction(session, transaction, reason):
    """Owner opts in around its existing commit boundary; no financial behavior."""
    try:
        async with transaction:
            yield
            changes = capture_accounts(session)
        publish_accounts(changes, reason)
    except BaseException:
        session.info.pop('trading_account_changes', None)
        raise


def enqueue_order_result(user_id: int, payload: FxOrderResult) -> None:
    """Offer committed FX outcomes only to the owner's existing subscriptions."""
    try:
        data = payload.model_dump(mode='json')
        for subscriber in tuple(EVENTS._subscribers.get(user_id, ())):
            subscriber.offer('fx_order_result', data)
    except Exception:
        logger.exception('Private FX order result dropped')
