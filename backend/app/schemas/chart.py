# app/schemas/chart.py

from datetime import datetime
from typing import Literal, Optional, List
from pydantic import BaseModel, Field


class PricePoint(BaseModel):
    ts: datetime
    price: float


class Candle(BaseModel):
    t: datetime  # bucket start
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0         # volume (shares)
    n: int = 0             # number of trades


Interval = Literal["10s", "30s", "1m", "5m", "15m", "1h", "1d"]


class PriceSeriesResponse(BaseModel):
    outcome_id: int
    from_ts: datetime
    to_ts: datetime
    points: List[PricePoint]


class CandleSeriesResponse(BaseModel):
    outcome_id: int
    interval: Interval
    from_ts: datetime
    to_ts: datetime
    candles: List[Candle]
