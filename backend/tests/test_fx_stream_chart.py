from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response

from app.api.v1.fx_stream import FX_THROUGH_HEADER, FX_VERSION_HEADER, chart


def _state(ready=True, version="v1", cursor=0):
    return SimpleNamespace(pair_id=3, last_trade_id=cursor, history_version=version,
                           history_ready=ready)


class _Result:
    def __init__(self, state):
        self._state = state

    def scalars(self):
        return self

    def first(self):
        return self._state

    def all(self):
        return []

    def scalar(self):
        return 0


class _JoinResult:
    """Result of the state LEFT OUTER JOIN candles statement."""

    def __init__(self, state, candles):
        self._state = state
        self._candles = candles

    def all(self):
        if self._candles:
            return [(self._state, candle) for candle in self._candles]
        return [(self._state, None)] if self._state is not None else []


class _CandleResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class FakeDB:
    def __init__(self, state=None, candles=None):
        self._state = state
        self._candles = candles or []

    async def get(self, model, pair_id):
        return object()

    async def execute(self, stmt):
        text = str(stmt)
        if "fx_market_data_state" in text and "fx_candle" in text:
            return _JoinResult(self._state, self._candles)
        if "fx_market_data_state" in text:
            return _Result(self._state)
        if "fx_candle" in text:
            return _CandleResult(self._candles)
        return _Result(None)


@pytest.mark.asyncio
async def test_chart_rejects_equal_range_before_query():
    moment = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)

    with pytest.raises(HTTPException) as exc:
        await chart(3, Response(), from_=moment, to=moment, db=object())

    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_chart_rejects_reversed_range_before_query():
    start = datetime(2026, 9, 28, 12, 1, tzinfo=timezone.utc)
    end = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)

    with pytest.raises(HTTPException) as exc:
        await chart(3, Response(), from_=start, to=end, db=object())

    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_chart_rejects_mixed_timezone_equal_range_before_query():
    start = datetime(2026, 9, 28, 12, 0)
    end = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)

    with pytest.raises(HTTPException) as exc:
        await chart(3, Response(), from_=start, to=end, db=object())

    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_chart_rejects_unknown_interval_and_bucket_ceiling_before_query():
    start = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)

    with pytest.raises(HTTPException) as exc:
        await chart(3, Response(), interval="1x", from_=start,
                    to=start + timedelta(minutes=5), db=object())
    assert exc.value.status_code == 422

    # 10s over three days is ~25,920 buckets, over the 20,000 ceiling.
    with pytest.raises(HTTPException) as exc:
        await chart(3, Response(), interval="10s", from_=start,
                    to=start + timedelta(days=3), db=object())
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_chart_normalizes_mixed_timezone_range_and_returns_empty_with_meta():
    start = datetime(2026, 9, 28, 12, 0)
    end = datetime(2026, 9, 28, 13, 0, tzinfo=timezone.utc)
    response = Response()
    result = await chart(3, response, interval="1m", from_=start, to=end,
                         db=FakeDB(state=_state(version="v1")))
    assert result == []
    assert response.headers[FX_THROUGH_HEADER] == "0"
    assert response.headers[FX_VERSION_HEADER] == "v1"


@pytest.mark.asyncio
async def test_chart_503_when_history_not_ready():
    start = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    db = FakeDB(state=_state(ready=False))
    with pytest.raises(HTTPException) as exc:
        await chart(3, Response(), interval="1m", from_=start,
                    to=start + timedelta(minutes=5), db=db)
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_chart_rolls_legacy_interval_up_and_reports_coverage():
    """Legacy ``2m`` has no materialised column; it rolls up the 1m base exactly."""
    start = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    rows = [
        SimpleNamespace(pair_id=3, interval="1m", bucket_start=start,
                        open_price=Decimal("1.10"), high_price=Decimal("1.20"),
                        low_price=Decimal("1.05"), close_price=Decimal("1.15"),
                        gold_volume=Decimal("2"), n_trades=1,
                        first_trade_at=start, first_trade_id=1,
                        last_trade_at=start, last_trade_id=1),
        SimpleNamespace(pair_id=3, interval="1m", bucket_start=start + timedelta(minutes=1),
                        open_price=Decimal("1.15"), high_price=Decimal("1.30"),
                        low_price=Decimal("1.12"), close_price=Decimal("1.25"),
                        gold_volume=Decimal("3"), n_trades=2,
                        first_trade_at=start + timedelta(minutes=1), first_trade_id=2,
                        last_trade_at=start + timedelta(minutes=1), last_trade_id=3),
    ]
    response = Response()
    db = FakeDB(state=_state(version="gen-2", cursor=3), candles=rows)
    result = await chart(3, response, interval="2m", from_=start,
                         to=start + timedelta(minutes=4), db=db)
    assert len(result) == 1
    assert result[0]["interval"] == "2m"
    assert result[0]["open"] == Decimal("1.10")
    assert result[0]["close"] == Decimal("1.25")
    assert result[0]["high"] == Decimal("1.30")
    assert result[0]["low"] == Decimal("1.05")
    assert result[0]["volume"] == Decimal("5")
    assert response.headers[FX_VERSION_HEADER] == "gen-2"
    assert response.headers[FX_THROUGH_HEADER] == "3"
