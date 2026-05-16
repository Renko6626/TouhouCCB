"""INTERVAL_ROUTE + _rollup 单元测试。

注：测试都用 async def + @pytest.mark.asyncio，即使函数体是纯同步逻辑。
原因：conftest.py 的 setup_db 是 async autouse fixture，sync test 撞它
会让 pytest-asyncio 1.3.0 hang。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.api.v1.chart import INTERVAL_ROUTE, _rollup, _INTERVAL_SECONDS


@pytest.mark.asyncio
async def test_interval_route_completeness():
    """所有现有 _INTERVAL_SECONDS 的 interval 都必须在 INTERVAL_ROUTE 中有映射。"""
    for k in _INTERVAL_SECONDS.keys():
        assert k in INTERVAL_ROUTE, f"interval {k} 缺少 INTERVAL_ROUTE 映射"


@pytest.mark.asyncio
async def test_interval_route_divisibility():
    """rollup_factor 必须满足 target_step / storage_step。"""
    for target, (storage, factor) in INTERVAL_ROUTE.items():
        target_secs = _INTERVAL_SECONDS[target]
        storage_secs = _INTERVAL_SECONDS[storage]
        assert target_secs == storage_secs * factor, (
            f"{target} 路由破坏整除：target_secs={target_secs}, "
            f"storage_secs={storage_secs}, factor={factor}"
        )


def _make_fine_candle(bucket_start: datetime, o, h, l, c, v=0, n=0):
    return SimpleNamespace(
        bucket_start=bucket_start,
        open_price=Decimal(str(o)),
        high_price=Decimal(str(h)),
        low_price=Decimal(str(l)),
        close_price=Decimal(str(c)),
        volume_shares=Decimal(str(v)),
        n_trades=n,
    )


@pytest.mark.asyncio
async def test_rollup_three_buckets_into_one():
    """3 个 10s 桶合 1 个 30s 桶：O 取首、C 取尾、H/L 极值、V/n 求和。"""
    anchor = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    fine = [
        _make_fine_candle(datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc),
                          o=0.5, h=0.55, l=0.48, c=0.52, v=10, n=2),
        _make_fine_candle(datetime(2026, 5, 17, 12, 0, 10, tzinfo=timezone.utc),
                          o=0.52, h=0.60, l=0.50, c=0.58, v=5, n=1),
        _make_fine_candle(datetime(2026, 5, 17, 12, 0, 20, tzinfo=timezone.utc),
                          o=0.58, h=0.59, l=0.45, c=0.46, v=3, n=1),
    ]
    rolled = _rollup(fine, target_step=30, anchor=anchor)
    assert len(rolled) == 1
    c = rolled[0]
    assert c.bucket_start == anchor
    assert c.open_price  == Decimal("0.5")
    assert c.close_price == Decimal("0.46")
    assert c.high_price  == Decimal("0.60")
    assert c.low_price   == Decimal("0.45")
    assert c.volume_shares == Decimal("18")
    assert c.n_trades == 4


@pytest.mark.asyncio
async def test_rollup_partial_bucket_group():
    """6 个 10s 桶 (=2 个完整 30s)：返回 2 行。"""
    anchor = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    fine = [
        _make_fine_candle(datetime(2026, 5, 17, 12, 0, t, tzinfo=timezone.utc),
                          o=0.5, h=0.5, l=0.5, c=0.5, v=1, n=1)
        for t in (0, 10, 20, 30, 40, 50)
    ]
    rolled = _rollup(fine, target_step=30, anchor=anchor)
    assert len(rolled) == 2
    assert rolled[0].bucket_start == datetime(2026, 5, 17, 12, 0, 0,  tzinfo=timezone.utc)
    assert rolled[1].bucket_start == datetime(2026, 5, 17, 12, 0, 30, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_rollup_empty_input():
    """空输入 → 空输出，不抛。"""
    anchor = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    assert _rollup([], target_step=30, anchor=anchor) == []


@pytest.mark.asyncio
async def test_rollup_single_bucket_factor_24():
    """24 个 1h 桶合 1 个 1d 桶（factor=24）。"""
    anchor = datetime(2026, 5, 17, 0, 0, 0, tzinfo=timezone.utc)
    fine = [
        _make_fine_candle(datetime(2026, 5, 17, hour, 0, 0, tzinfo=timezone.utc),
                          o=0.4 + hour * 0.01,
                          h=0.5 + hour * 0.01,
                          l=0.3 + hour * 0.01,
                          c=0.45 + hour * 0.01,
                          v=hour, n=1)
        for hour in range(24)
    ]
    rolled = _rollup(fine, target_step=86400, anchor=anchor)
    assert len(rolled) == 1
    c = rolled[0]
    assert c.open_price == Decimal("0.4")
    assert c.high_price == Decimal("0.73")
    assert c.low_price  == Decimal("0.3")
    assert c.close_price == Decimal("0.68")
    assert c.volume_shares == Decimal(sum(range(24)))
    assert c.n_trades == 24
