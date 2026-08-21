"""HistoryRing 纯内存单元测试（无 DB、无 asyncio）。"""
from datetime import datetime, timezone
from decimal import Decimal

from app.services.history_ring import RING_SPEC, HistoryRing, seal_boundary


def _row(interval="10s", epoch=1_755_734_400, o="0.5", h="0.6", l="0.5", c="0.6",
         v="1", n=1, oid=1):
    return {
        "outcome_id": oid, "interval": interval,
        "bucket_start": datetime.fromtimestamp(epoch, tz=timezone.utc),
        "open_price": Decimal(o), "high_price": Decimal(h),
        "low_price": Decimal(l), "close_price": Decimal(c),
        "volume_shares": Decimal(v), "n_trades": n,
        "updated_at": datetime.fromtimestamp(epoch, tz=timezone.utc),
    }


def test_ring_spec_matches_design():
    assert RING_SPEC["10s"].step == 10 and RING_SPEC["10s"].buckets == 360 and RING_SPEC["10s"].segment == 600
    assert RING_SPEC["1m"].step == 60 and RING_SPEC["1m"].buckets == 1440 and RING_SPEC["1m"].segment == 3600
    assert RING_SPEC["15m"].step == 900 and RING_SPEC["15m"].buckets == 672 and RING_SPEC["15m"].segment == 86400
    assert RING_SPEC["1h"].step == 3600 and RING_SPEC["1h"].buckets == 2160 and RING_SPEC["1h"].segment == 604800


def test_merge_same_bucket_open_first_close_last():
    r = HistoryRing()
    r.merge_row(_row(o="0.5", h="0.6", l="0.5", c="0.6", v="1", n=1))
    r.merge_row(_row(o="0.6", h="0.7", l="0.4", c="0.55", v="2", n=1))
    seg = r.get_segment("10s", 1_755_734_400)   # 1_755_734_400 % 600 == 0
    assert seg["t"] == [0]
    assert seg["o"] == [round(0.5 * 1e8)]
    assert seg["c"] == [round(0.55 * 1e8)]
    assert seg["h"] == [round(0.7 * 1e8)]
    assert seg["l"] == [round(0.4 * 1e8)]
    assert seg["v"] == [3.0]
    assert seg["trades"] == [2]


def test_segment_encoding_sparse_and_shape():
    r = HistoryRing()
    base = 1_755_734_400
    r.merge_row(_row(epoch=base))                # 桶 0
    r.merge_row(_row(epoch=base + 30))           # 桶 3
    seg = r.get_segment("10s", base)
    assert seg["t0"] == base and seg["step"] == 10 and seg["n_buckets"] == 60
    assert seg["t"] == [0, 3]
    assert len(seg["o"]) == len(seg["h"]) == len(seg["l"]) == len(seg["c"]) == len(seg["v"]) == len(seg["trades"]) == 2
    # 段外的桶不进本段
    r.merge_row(_row(epoch=base + 600))
    assert r.get_segment("10s", base)["t"] == [0, 3]


def test_empty_segment_is_valid_encoding_not_none():
    r = HistoryRing()
    seg = r.get_segment("1m", 1_755_734_400 - 1_755_734_400 % 3600)
    assert seg["t"] == [] and seg["o"] == []
    assert seg["n_buckets"] == 60


def test_window_pruning_drops_old_buckets():
    r = HistoryRing()
    base = 1_755_734_400
    r.merge_row(_row(epoch=base))
    # 推进超过 1h 窗口（360 桶 × 10s）
    r.merge_row(_row(epoch=base + 360 * 10))
    assert r.get_segment("10s", base)["t"] == []          # 老桶已被修剪
    # 其它档不受影响（各档独立窗口）
    r.merge_row(_row(interval="1h", epoch=base - base % 3600))
    assert r.get_segment("1h", (base - base % 3600) - (base - base % 3600) % 604800)["t"] != []


def test_tail_and_window_start_and_seal_boundary():
    r = HistoryRing()
    base = 1_755_734_400            # 对齐 600
    now = base + 250                # 段内进行中
    assert seal_boundary("10s", now) == base
    r.merge_row(_row(epoch=base + 240))
    t = r.tail("10s", now)
    assert t["t0"] == base and t["t"] == [24]
    assert r.window_start("10s", now) == (now - now % 10) - 359 * 10
