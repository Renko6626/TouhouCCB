"""Public FX chart aggregation and realtime payloads."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable

from app.services.credit.keys import symbol_namespace
from app.services.realtime import MarketEventBroker

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FxCandle:
    bucket_start: datetime
    interval: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


def _interval_seconds(interval: str) -> int:
    value = str(interval).strip().lower()
    units = {"m": 60, "h": 3600, "d": 86400}
    if len(value) < 2 or value[-1] not in units:
        raise ValueError("interval must be Nm, Nh, or Nd")
    try:
        amount = int(value[:-1])
    except ValueError as exc:
        raise ValueError("interval must be Nm, Nh, or Nd") from exc
    if amount <= 0 or amount > 1440:
        raise ValueError("interval is out of range")
    return amount * units[value[-1]]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _bucket(ts: datetime, seconds: int) -> datetime:
    ts = _utc(ts)
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, tz=timezone.utc)


def _volume(trade: Any) -> Decimal:
    # Gold-side volume matches the public 24h snapshot convention.
    return Decimal(trade.input_amount if trade.side == "buy" else trade.output_amount)


def build_price_buckets(trades: Iterable[Any], interval: str,
                        start: datetime, end: datetime) -> list[FxCandle]:
    seconds = _interval_seconds(interval)
    start, end = _utc(start), _utc(end)
    buckets: dict[datetime, list[Any]] = {}
    for trade in sorted(trades, key=lambda row: (_utc(row.created_at), row.id or 0)):
        ts = _utc(trade.created_at)
        if not (start <= ts < end):
            continue
        buckets.setdefault(_bucket(ts, seconds), []).append(trade)
    result: list[FxCandle] = []
    for bucket_start, rows in sorted(buckets.items()):
        prices = [Decimal(row.post_price) for row in rows]
        result.append(FxCandle(
            bucket_start=bucket_start, interval=interval,
            open=prices[0], high=max(prices), low=min(prices), close=prices[-1],
            volume=sum((_volume(row) for row in rows), Decimal("0")),
        ))
    return result


_FRAME_KEYS = ("price", "buy_price", "sell_price", "spread", "volume")
_NEWS_KEYS = ("title", "body", "kind", "published_at")
#: Additive SSE envelope fields (frozen with the frontend in task-5).  The old
#: quote/news frame stays byte-identical when the runtime has no tail/deltas.
_HISTORY_SCALAR_KEYS = (
    "history_version", "history_ready", "history_tail_at",
    "history_tail_through_trade_id",
)
_SEGMENT_KEYS = ("t0", "step", "n_buckets", "t", "o", "h", "l", "c", "v", "trades")
_TRADE_KEYS = ("id", "ts", "post_price", "gold_volume")


def build_public_frame(snapshot: Any, news: Any = None) -> dict[str, Any]:
    def get(name: str, default: Any = None) -> Any:
        return snapshot.get(name, default) if isinstance(snapshot, dict) else getattr(snapshot, name, default)

    frame: dict[str, Any] = {
        "price": get("price"),
        "spread": get("spread"),
        "volume": get("volume", get("volume_24h", Decimal("0"))),
    }
    # Quotes are useful to clients when available, but no internal snapshot key
    # is copied wholesale into the wire frame.
    for key in ("buy_price", "sell_price"):
        if get(key) is not None:
            frame[key] = get(key)
    if news:
        frame["news"] = {key: (news.get(key) if isinstance(news, dict) else getattr(news, key, None))
                          for key in _NEWS_KEYS if (news.get(key) if isinstance(news, dict) else getattr(news, key, None)) is not None}
    return frame


def build_public_envelope(snapshot: Any, *, history: dict[str, Any] | None = None,
                          trades: Iterable[dict[str, Any]] | None = None,
                          history_invalidated: bool = False) -> dict[str, Any]:
    """Old quote/news frame plus the additive history/delta envelope fields.

    Only the fields we produced or explicitly allowlisted are copied; private
    system state never rides along.
    """
    frame = build_public_frame(snapshot)
    if history:
        for key in ("history_version", "history_ready", "history_tail",
                    "history_tail_at", "history_tail_through_trade_id"):
            if history.get(key) is not None:
                frame[key] = history[key]
    if trades:
        frame["trades"] = list(trades)
    if history_invalidated:
        frame["history_invalidated"] = True
    return frame


def _wire_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _wire_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_wire_value(item) for item in value]
    return value


def _wire_segment(segment: Any) -> dict[str, Any] | None:
    if not isinstance(segment, dict):
        return None
    wired = {key: _wire_value(segment[key]) for key in _SEGMENT_KEYS if key in segment}
    return wired if "t0" in wired and "step" in wired else None


def _wire_tail(tail: Any) -> dict[str, Any] | None:
    if not isinstance(tail, dict):
        return None
    wired = {interval: segment for interval, value in tail.items()
             if (segment := _wire_segment(value)) is not None}
    return wired or None


def _wire_trade(trade: Any) -> dict[str, Any] | None:
    if not isinstance(trade, dict):
        return None
    wired = {key: _wire_value(trade[key]) for key in _TRADE_KEYS if key in trade}
    return wired if "id" in wired and "ts" in wired else None


def public_frame_to_wire(frame: dict[str, Any]) -> dict[str, Any]:
    """Convert an allowlisted frame to JSON-safe values at the wire boundary.

    The allowlist is retained while recursing into the nested tail map and the
    trade array, so no private runtime field can be spread onto the wire.
    """
    public: dict[str, Any] = {}
    for key in _FRAME_KEYS:
        if key in frame:
            public[key] = _wire_value(frame[key])
    if "news" in frame and isinstance(frame["news"], dict):
        public["news"] = {key: _wire_value(frame["news"][key]) for key in _NEWS_KEYS
                          if key in frame["news"]}
    for key in _HISTORY_SCALAR_KEYS:
        if key in frame:
            public[key] = _wire_value(frame[key])
    if "history_tail" in frame:
        tail = _wire_tail(frame["history_tail"])
        if tail:
            public["history_tail"] = tail
    if frame.get("trades"):
        trades = [wired for trade in frame["trades"]
                  if (wired := _wire_trade(trade)) is not None]
        if trades:
            public["trades"] = trades
    if frame.get("history_invalidated"):
        public["history_invalidated"] = True
    return public


async def publish_public_frame(broker: MarketEventBroker, pair_id: int,
                               frame: dict[str, Any]) -> None:
    # WP8a：FX 帧只进 ``fx:{pair_id}`` lane，绝不与同号 LMSR market 串流。
    await broker.publish(symbol_namespace("fx", int(pair_id)), "fx",
                         public_frame_to_wire(frame))


async def publish_pair_frame(pair_id: int, post_price: Any,
                             broker: MarketEventBroker | None = None) -> None:
    """Publish one committed post-trade frame for a pair.

    Reads the committed snapshot in its own session, so callers must only
    invoke it after commit.  ``post_price`` is the event price the frame
    carries.  This is the function the background publisher calls after its
    subscriber gate, so a frame is only built when someone is watching.

    With a witness the runtime is advanced **incrementally** when this process
    owns the writer (``catch_up`` only queues new trades; a read-only instance
    is left untouched) and its bounded, discardable public-trade buffer is
    drained exactly once.  The frame then combines the current quote, the small
    tail metadata and those deltas.  A buffer overflow surfaces as an explicit
    ``history_invalidated`` so the client refetches only the tail.
    """
    if broker is None:
        from app.services.realtime import BROKER
        broker = BROKER
    # Read after commit so the frame agrees with the initial snapshot's
    # cumulative 24h volume and includes the current public quote fields.
    from app.core.database import async_session_maker
    from app.services.fx import trading
    from app.services.fx.market_state import FX_MARKET_DATA

    async with async_session_maker() as db:
        snapshot = await trading.get_public_snapshot(db, pair_id)

    history: dict[str, Any] | None = None
    trades: list[dict[str, Any]] = []
    invalidated = False
    runtime = FX_MARKET_DATA
    if runtime is not None:
        if runtime.write_owner:
            try:
                await runtime.catch_up(int(pair_id))
            except Exception:  # noqa: BLE001 - publication must never fail a trade
                logger.exception("fx runtime catch-up before publication failed for pair %s",
                                 pair_id)
        trades, invalidated = runtime.drain_public_trades(int(pair_id))
        state = runtime.state(int(pair_id))
        if state is not None:
            history = {
                "history_version": state["history_version"],
                "history_ready": state["history_ready"],
                "history_tail_at": datetime.now(timezone.utc).isoformat(),
                "history_tail_through_trade_id": state["applied_trade_id"],
            }

    frame = build_public_envelope(snapshot, history=history, trades=trades,
                                  history_invalidated=invalidated)
    # The trade's post marginal price is the event price; snapshot quotes and
    # cumulative volume describe the committed state around that trade.
    frame["price"] = Decimal(post_price)
    await publish_public_frame(broker, pair_id, frame)


async def publish_trade(trade: Any, broker: MarketEventBroker | None = None) -> None:
    """Publish a post-commit trade frame; user/system identity is never exposed.

    With an explicit ``broker`` (isolated tests and direct callers) the frame is
    written to that broker immediately.  Production callers pass no broker: the
    committed values are handed to the bounded coalescing
    :data:`app.services.fx.publisher.PUBLISHER`, which never blocks, skips pairs
    without subscribers and cannot fail or slow the transaction that produced
    the trade.
    """
    if broker is None:
        from app.services.fx import publisher

        publisher.enqueue_publication(
            pair_id=trade.pair_id,
            post_price=Decimal(trade.post_price),
            trade_id=getattr(trade, "id", None),
        )
        return
    await publish_pair_frame(trade.pair_id, trade.post_price, broker)
