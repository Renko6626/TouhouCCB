"""Ephemeral private hints. Delivery failure never participates in money commits."""
import asyncio
import json
import logging
from dataclasses import dataclass, field
from uuid import uuid4
from app.services.trading_prices import now_string
from app.schemas.fx import FxOrderResult

logger = logging.getLogger(__name__)

def encode_frame(event: str, payload) -> bytes:
    """Freeze private SSE content once, using the existing compact JSON format."""
    return ('event: ' + event + '\ndata: ' +
            json.dumps(payload, separators=(',', ':')) + '\n\n').encode('utf-8')


@dataclass(eq=False)
class Subscription:
    user_id: int
    queue: asyncio.Queue[tuple[str, bytes]] = field(default_factory=lambda: asyncio.Queue(maxsize=64))
    closed: asyncio.Event = field(default_factory=asyncio.Event)

    def offer(self, event, payload):
        if not self.closed.is_set():
            self.offer_encoded(event, encode_frame(event, payload))

    def offer_encoded(self, event: str, wire: bytes):
        """Offer a shared immutable frame to this connection's existing FIFO."""
        if self.closed.is_set():
            return
        try:
            self.queue.put_nowait((event, wire))
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
        subscribers = tuple(self._subscribers.get(user_id, ()))
        if not subscribers:
            return
        payload = dict(event_id=str(uuid4()), economic_version=str(economic_version), reason=reason, occurred_at=now_string())
        if operation_ref is not None:
            payload['operation_ref'] = str(operation_ref)
        wire = encode_frame('account_changed', payload)
        for subscriber in subscribers:
            subscriber.offer_encoded('account_changed', wire)

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
async def notifying_transaction(session, transaction, reason, *, defer_publication=False):
    """Yield committed account descriptors; writers can publish after installing prices."""
    # Returned outcomes may hold this list even on an early return from the body.
    # Populate it only after the transaction context has committed successfully.
    committed_changes = []
    try:
        async with transaction:
            yield committed_changes
            changes = capture_accounts(session)
        committed_changes.extend(changes)
        if not defer_publication:
            publish_accounts(committed_changes, reason)
    except BaseException:
        session.info.pop('trading_account_changes', None)
        raise


def enqueue_order_result(user_id: int, payload: FxOrderResult) -> None:
    """Offer committed FX outcomes only to the owner's existing subscriptions."""
    try:
        subscribers = tuple(EVENTS._subscribers.get(user_id, ()))
        if not subscribers:
            return
        wire = encode_frame('fx_order_result', payload.model_dump(mode='json'))
        for subscriber in subscribers:
            subscriber.offer_encoded('fx_order_result', wire)
    except Exception:
        logger.exception('Private FX order result dropped')
