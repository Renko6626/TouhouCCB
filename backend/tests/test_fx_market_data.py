from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from app.services.fx.market_data import build_price_buckets, build_public_frame


def _trade(ts, price, amount="2", *, source="player", user_id=7):
    return SimpleNamespace(
        id=1, pair_id=3, created_at=ts, post_price=Decimal(price),
        input_amount=Decimal(amount), output_amount=Decimal("1"),
        side="buy", source=source, user_id=user_id,
    )


def test_price_buckets_use_post_price_ohlcv_and_include_system_trades():
    start = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    trades = [
        _trade(datetime(2026, 9, 28, 12, 0, 5, tzinfo=timezone.utc), "1.10"),
        _trade(datetime(2026, 9, 28, 12, 0, 20, tzinfo=timezone.utc), "1.20", "3"),
        _trade(datetime(2026, 9, 28, 12, 1, 2, tzinfo=timezone.utc), "1.05", "4", source="system", user_id=None),
    ]

    candles = build_price_buckets(trades, "1m", start, datetime(2026, 9, 28, 12, 2, tzinfo=timezone.utc))

    assert len(candles) == 2
    assert candles[0].open == Decimal("1.10")
    assert candles[0].high == Decimal("1.20")
    assert candles[0].low == Decimal("1.10")
    assert candles[0].close == Decimal("1.20")
    assert candles[0].volume == Decimal("5")
    assert candles[1].open == candles[1].close == Decimal("1.05")
    assert candles[1].volume == Decimal("4")


def test_public_frame_allowlists_market_fields_and_news():
    snapshot = {
        "price": Decimal("1.1"), "buy_price": Decimal("1.2"),
        "sell_price": Decimal("1.0"), "spread": Decimal("0.2"),
        "volume_24h": Decimal("4"), "target_price": Decimal("9"),
        "shock_ratio": Decimal("0.5"), "random_state": "secret",
    }
    frame = build_public_frame(snapshot, {"title": "公开新闻", "body": "说明", "kind": "macro"})

    assert frame["price"] == Decimal("1.1")
    assert frame["spread"] == Decimal("0.2")
    assert frame["volume"] == Decimal("4")
    assert frame["news"]["title"] == "公开新闻"
    assert "target_price" not in frame
    assert "shock_ratio" not in frame
    assert "random_state" not in frame


def test_price_buckets_use_sell_output_for_volume():
    start = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    trade = _trade(start.replace(second=5), "1.10", "8")
    trade.side = "sell"
    trade.output_amount = Decimal("3.25")

    candles = build_price_buckets([trade], "1m", start, start + timedelta(minutes=1))

    assert candles[0].volume == Decimal("3.25")


def test_incremental_candle_rows_match_legacy_1m_ohlcv():
    """The durable aggregation must reproduce the chart's FX 口径 for 1m.

    Guards against the new incremental path silently drifting from the legacy
    ``build_price_buckets`` OHLCV (post_price order, buy-input/sell-output gold
    volume) while the read path is being migrated.
    """
    from app.services.fx.candles import compute_fx_candle_rows

    start = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    trades = [
        _trade(start.replace(second=20), "1.20", "3"),
        _trade(start.replace(second=5), "1.10", "2"),
        _trade(start.replace(minute=1, second=2), "1.05", "4", source="system", user_id=None),
    ]
    trades[0].side = "sell"
    trades[0].output_amount = Decimal("0.75")

    legacy = build_price_buckets(
        trades, "1m", start, start + timedelta(minutes=2),
    )
    rows = compute_fx_candle_rows(trades)
    incremental = {
        row["bucket_start"]: row for row in rows if row["interval"] == "1m"
    }

    assert set(incremental) == {c.bucket_start for c in legacy}
    for candle in legacy:
        row = incremental[candle.bucket_start]
        assert Decimal(row["open_price"]) == candle.open
        assert Decimal(row["high_price"]) == candle.high
        assert Decimal(row["low_price"]) == candle.low
        assert Decimal(row["close_price"]) == candle.close
        assert Decimal(row["gold_volume"]) == candle.volume
