"""Public FX chart aggregation and realtime payloads."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable

from app.services.credit.keys import symbol_namespace
from app.services.realtime import MarketEventBroker


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


def public_frame_to_wire(frame: dict[str, Any]) -> dict[str, Any]:
    """Convert an allowlisted frame to JSON-safe values at the wire boundary."""
    public = {key: frame[key] for key in _FRAME_KEYS if key in frame}
    if "news" in frame and isinstance(frame["news"], dict):
        public["news"] = {key: frame["news"][key] for key in _NEWS_KEYS
                           if key in frame["news"]}

    def wire(value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {key: wire(item) for key, item in value.items()}
        return value
    return wire(public)


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
    carries.  This is the function the background publisher calls; keeping it
    separate from ``publish_trade`` lets the post-commit path receive plain
    values instead of an ORM row that may already be expired.
    """
    if broker is None:
        from app.services.realtime import BROKER
        broker = BROKER
    # Read after commit so the frame agrees with the initial snapshot's
    # cumulative 24h volume and includes the current public quote fields.
    from app.core.database import async_session_maker
    from app.services.fx import trading

    async with async_session_maker() as db:
        snapshot = await trading.get_public_snapshot(db, pair_id)
    frame = build_public_frame(snapshot)
    # The trade's post marginal price is the event price; snapshot quotes and
    # cumulative volume describe the committed state around that trade.
    frame["price"] = Decimal(post_price)
    await publish_public_frame(broker, pair_id, frame)


async def publish_trade(trade: Any, broker: MarketEventBroker | None = None) -> None:
    """Publish a post-commit trade frame; user/system identity is never exposed."""
    await publish_pair_frame(trade.pair_id, trade.post_price, broker)
