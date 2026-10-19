"""Ephemeral public round notifications; persisted state is always authoritative."""
import asyncio
import logging

from app.services.account_events import encode_frame

logger = logging.getLogger(__name__)
_subscribers: set[asyncio.Queue] = set()


def subscribe():
    if len(_subscribers) >= 500:
        raise RuntimeError('竞猜订阅人数已满')
    queue = asyncio.Queue(maxsize=16)
    _subscribers.add(queue)
    return queue


def unsubscribe(queue):
    _subscribers.discard(queue)


def publish(round_key, status):
    # This transient hint must never turn an already committed payout into an error.
    try:
        wire = encode_frame('round_changed', {'round_key': round_key, 'status': status})
        for queue in tuple(_subscribers):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(wire)
    except Exception:
        logger.exception('Returned round hint dropped')
