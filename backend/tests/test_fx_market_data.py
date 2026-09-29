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
