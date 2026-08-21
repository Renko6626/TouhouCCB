"""Candle 写入的独立 flusher（spec § 7.5）。

writer 每笔成交把 compute_candle_rows 的输出 merge 进内存 pending（微秒），
flusher 每 5 s 批量 UPSERT。崩溃最多丢 5 s，由 main.py::_resync_recent_candles
启动兜底重放补齐（窗口 1 h >> 5 s，无需调参）。

pop-then-upsert 语义：flush 先原子取走整批再落库；失败把这批按"较早方"
merge 回 pending——因为 upsert_candles 的 volume/n 是累加合并，同一批 flush
两次会 double-count，绝不能 flush 成功后不清 / 失败后重复 flush 同批。
"""
from __future__ import annotations

import asyncio
import logging

from app.core.database import async_session_maker
from app.services.candle_writer import upsert_candles

logger = logging.getLogger(__name__)

_Key = tuple[int, str, object]   # (outcome_id, interval, bucket_start)


def _key(row: dict) -> _Key:
    return (row["outcome_id"], row["interval"], row["bucket_start"])


def _merge_row(earlier: dict, later: dict) -> dict:
    """同桶合并，earlier 在时间上先发生：open 保留 earlier，close 取 later。"""
    return {
        **later,
        "open_price": earlier["open_price"],
        "high_price": max(earlier["high_price"], later["high_price"]),
        "low_price": min(earlier["low_price"], later["low_price"]),
        "close_price": later["close_price"],
        "volume_shares": earlier["volume_shares"] + later["volume_shares"],
        "n_trades": earlier["n_trades"] + later["n_trades"],
    }


class CandleFlusher:
    FLUSH_INTERVAL = 5.0

    def __init__(self) -> None:
        self._pending: dict[_Key, dict] = {}
        self._task: asyncio.Task | None = None

    def pending_count(self) -> int:
        return len(self._pending)

    def merge(self, rows: list[dict]) -> None:
        for row in rows:
            k = _key(row)
            old = self._pending.get(k)
            self._pending[k] = _merge_row(old, row) if old else dict(row)

    async def flush_once(self) -> int:
        if not self._pending:
            return 0
        batch = self._pending
        self._pending = {}
        try:
            async with async_session_maker() as s:
                await upsert_candles(s, list(batch.values()))
                await s.commit()
            return len(batch)
        except Exception:
            logger.exception("candle flush failed, re-merging %d rows", len(batch))
            # 回炉：batch 是较早方（失败期间可能有新 merge 进来）
            for k, row in batch.items():
                newer = self._pending.get(k)
                self._pending[k] = _merge_row(row, newer) if newer else row
            return 0

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="candle-flusher")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.flush_once()   # 最终 flush，优雅停机不丢

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.FLUSH_INTERVAL)
            try:
                await self.flush_once()
            except Exception:
                logger.exception("candle flusher loop error")


CANDLE_FLUSHER = CandleFlusher()
