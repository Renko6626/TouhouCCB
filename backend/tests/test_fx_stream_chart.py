from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.api.v1.fx_stream import chart


@pytest.mark.asyncio
async def test_chart_rejects_equal_range_before_query():
    moment = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)

    with pytest.raises(HTTPException) as exc:
        await chart(3, from_=moment, to=moment, db=object())

    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_chart_rejects_reversed_range_before_query():
    start = datetime(2026, 9, 28, 12, 1, tzinfo=timezone.utc)
    end = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)

    with pytest.raises(HTTPException) as exc:
        await chart(3, from_=start, to=end, db=object())

    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_chart_rejects_mixed_timezone_equal_range_before_query():
    start = datetime(2026, 9, 28, 12, 0)
    end = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)

    with pytest.raises(HTTPException) as exc:
        await chart(3, from_=start, to=end, db=object())

    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_chart_normalizes_mixed_timezone_range_before_query(monkeypatch):
    class FakeDB:
        async def get(self, model, pair_id):
            return object()

        async def execute(self, stmt):
            class Result:
                def scalars(self):
                    return self

                def all(self):
                    return []
            return Result()

    start = datetime(2026, 9, 28, 12, 0)
    end = datetime(2026, 9, 28, 13, 0, tzinfo=timezone.utc)
    result = await chart(3, interval="1m", from_=start, to=end, db=FakeDB())
    assert result == []
