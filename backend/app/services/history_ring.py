"""每 outcome 的内存多分辨率环形缓冲（spec § 7.1 / § 7.2 / § 7.3）。

writer 每笔 commit 后把 compute_candle_rows 的行 merge 进来（与 candle
flusher 同一份数据），/history/ 端点与 SSE snapshot 从这里读。全部操作
在同一个 event loop 内、读取期间无 await，不需要锁。

价格在编码边界转 8 位定点整数（round(float * 1e8)）——这是 spec § 7.1
的列式编码契约，客户端 ÷1e8 还原。桶内部保留 Decimal 精确合并。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict


@dataclass(frozen=True)
class RingTier:
    step: int      # 桶宽（秒）
    buckets: int   # ring 容量（桶数）
    segment: int   # 封存段长（秒），spec § 7.2

    @property
    def window(self) -> int:
        return self.step * self.buckets


RING_SPEC: dict[str, RingTier] = {
    "10s": RingTier(step=10, buckets=360, segment=600),
    "1m": RingTier(step=60, buckets=1440, segment=3600),
    "15m": RingTier(step=900, buckets=672, segment=86400),
    "1h": RingTier(step=3600, buckets=2160, segment=604800),
}


def seal_boundary(interval: str, now_epoch: int) -> int:
    """最近一个已过去的封存边界（<= now）。边界之前的段才不可变。"""
    seg = RING_SPEC[interval].segment
    return now_epoch - (now_epoch % seg)


_PRICE_FIXED = 1e8


class HistoryRing:
    """单 outcome 的 4 档 OHLCV 桶。桶存储：epoch → dict(o,h,l,c Decimal, v Decimal, n int)。"""

    def __init__(self) -> None:
        self._tiers: Dict[str, Dict[int, dict]] = {k: {} for k in RING_SPEC}

    def merge_row(self, row: dict) -> None:
        interval = row["interval"]
        tier = RING_SPEC.get(interval)
        if tier is None:
            return
        buckets = self._tiers[interval]
        epoch = int(row["bucket_start"].timestamp())
        b = buckets.get(epoch)
        if b is None:
            buckets[epoch] = {
                "o": row["open_price"], "h": row["high_price"],
                "l": row["low_price"], "c": row["close_price"],
                "v": row["volume_shares"], "n": int(row["n_trades"]),
            }
        else:
            # 同桶合并：open 保留最早（不动），close 取最新，h/l 极值，v/n 累加
            b["h"] = max(b["h"], row["high_price"])
            b["l"] = min(b["l"], row["low_price"])
            b["c"] = row["close_price"]
            b["v"] = b["v"] + row["volume_shares"]
            b["n"] = b["n"] + int(row["n_trades"])
        # 按窗口修剪：以本档最新桶为基准，丢弃滑出 ring 的老桶
        newest = max(buckets)
        floor = newest - (tier.buckets - 1) * tier.step
        if min(buckets) < floor:
            for e in [e for e in buckets if e < floor]:
                del buckets[e]

    def _encode(self, interval: str, t0: int, until_exclusive: int) -> dict:
        tier = RING_SPEC[interval]
        buckets = self._tiers[interval]
        epochs = sorted(e for e in buckets if t0 <= e < until_exclusive)
        out = {
            "t0": t0, "step": tier.step,
            "n_buckets": (until_exclusive - t0 + tier.step - 1) // tier.step,
            "t": [], "o": [], "h": [], "l": [], "c": [], "v": [], "trades": [],
        }
        for e in epochs:
            b = buckets[e]
            out["t"].append((e - t0) // tier.step)
            out["o"].append(round(float(b["o"]) * _PRICE_FIXED))
            out["h"].append(round(float(b["h"]) * _PRICE_FIXED))
            out["l"].append(round(float(b["l"]) * _PRICE_FIXED))
            out["c"].append(round(float(b["c"]) * _PRICE_FIXED))
            out["v"].append(float(b["v"]))
            out["trades"].append(b["n"])
        return out

    def get_segment(self, interval: str, segment_epoch: int) -> dict:
        seg = RING_SPEC[interval].segment
        enc = self._encode(interval, segment_epoch, segment_epoch + seg)
        enc["n_buckets"] = seg // RING_SPEC[interval].step
        return enc

    def tail(self, interval: str, now_epoch: int) -> dict:
        t0 = seal_boundary(interval, now_epoch)
        step = RING_SPEC[interval].step
        return self._encode(interval, t0, (now_epoch - now_epoch % step) + step)

    def window_start(self, interval: str, now_epoch: int) -> int:
        tier = RING_SPEC[interval]
        return (now_epoch - now_epoch % tier.step) - (tier.buckets - 1) * tier.step
