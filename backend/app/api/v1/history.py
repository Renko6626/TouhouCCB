"""/history/ 不可变历史段端点（spec § 7.2 / § 7.4 / D4）。

不挂在 /api/v1 下：main.py 的 _set_no_store_for_api 只对 /api/v1/ 打
no-store，本路由的 immutable 缓存头因此不被覆盖。nginx 对 /history/ 叠
proxy_cache（deploy/nginx.conf），回源次数 ≈ 段数，与在线人数无关。

三道封存防线——错一个 200 会被 nginx 钉 30 天、浏览器钉 1 年：
  1. 段末尾 > now → 404（进行中的段永不吐，尾巴走 SSE snapshot）
  2. writer on 且段在 ring 窗口内 → 一律 ring 供数，不读 DB（ring 是
     writer 实时写的，跨过封存边界即完整，无需等 flusher）
  3. 超窗 / writer off → 先验 flusher 高水位（oldest_pending_bucket），
     未覆盖段范围才从 DB 聚合，并进程内 LRU 缓存
"""
from __future__ import annotations

import json
import time
from collections import OrderedDict
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.base import Outcome, OutcomeCandle
from app.services.candle_flusher import CANDLE_FLUSHER
from app.services.history_ring import RING_SPEC, HistoryRing
from app.services.market_writer import WRITER

router = APIRouter()

IMMUTABLE_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}

# 进程内 LRU：key=(outcome_id, interval, segment_epoch) → 编码 dict。
# 段内容不可变，缓存永不失效；上限控内存，满了淘汰最久未用。
_LRU_MAX = 1024
_lru: OrderedDict[tuple[int, str, int], dict] = OrderedDict()


def _lru_get(key):
    enc = _lru.get(key)
    if enc is not None:
        _lru.move_to_end(key)
    return enc


def _lru_put(key, enc) -> None:
    _lru[key] = enc
    _lru.move_to_end(key)
    if len(_lru) > _LRU_MAX:
        _lru.popitem(last=False)


def _json_immutable(enc: dict) -> Response:
    return Response(
        content=json.dumps(enc, separators=(",", ":")),
        media_type="application/json",
        headers=IMMUTABLE_HEADERS,
    )


def _encode_db_rows(interval: str, segment_epoch: int, rows) -> dict:
    """DB 行 → 与 HistoryRing._encode 相同的列式编码（复用 ring 保证格式恒一致）。"""
    ring = HistoryRing()
    for c in rows:
        bs = c.bucket_start if c.bucket_start.tzinfo else c.bucket_start.replace(tzinfo=timezone.utc)
        ring.merge_row({
            "outcome_id": int(c.outcome_id), "interval": c.interval, "bucket_start": bs,
            "open_price": c.open_price, "high_price": c.high_price,
            "low_price": c.low_price, "close_price": c.close_price,
            "volume_shares": c.volume_shares, "n_trades": c.n_trades,
            "updated_at": c.updated_at,
        })
    return ring.get_segment(interval, segment_epoch)


@router.get("/o/{outcome_id}/{interval}/{segment_epoch}.json", summary="不可变历史段（列式 OHLCV）")
async def get_history_segment(outcome_id: int, interval: str, segment_epoch: int):
    tier = RING_SPEC.get(interval)
    if tier is None:
        raise HTTPException(status_code=404, detail="不支持的 interval")
    if segment_epoch < 0 or segment_epoch % tier.segment != 0:
        raise HTTPException(status_code=404, detail="segment_epoch 未对齐段长")

    now = int(time.time())
    seg_end = segment_epoch + tier.segment
    if seg_end > now:
        # 防线 1：进行中的段永不吐（尾巴数据只走 SSE snapshot，spec § 7.3）
        raise HTTPException(status_code=404, detail="段尚未封存")

    # 防线 2：ring 窗口内一律从 ring 供数（writer 实时写，跨过边界即完整）
    if WRITER.enabled:
        mid = WRITER.market_id_for_outcome(outcome_id)
        st = WRITER.get_state(mid) if mid is not None else None
        ring = st.rings.get(outcome_id) if st is not None else None
        if ring is not None and segment_epoch >= ring.window_start(interval, now):
            return _json_immutable(ring.get_segment(interval, segment_epoch))

    # 防线 3：超窗 / writer off → DB。段不可变，命中 LRU 直接回
    key = (outcome_id, interval, segment_epoch)
    cached = _lru_get(key)
    if cached is not None:
        return _json_immutable(cached)

    oldest_pending = CANDLE_FLUSHER.oldest_pending_bucket()
    if oldest_pending is not None:
        op = oldest_pending if oldest_pending.tzinfo else oldest_pending.replace(tzinfo=timezone.utc)
        if int(op.timestamp()) < seg_end:
            # 该段范围的 flush 尚未完成——不完整段绝不以 immutable 固化
            raise HTTPException(status_code=404, detail="段落库未完成，请稍后重试")

    async with async_session_maker() as s:
        if (await s.execute(select(Outcome.id).where(Outcome.id == outcome_id))).scalars().first() is None:
            raise HTTPException(status_code=404, detail="选项不存在")
        rows = (await s.execute(
            select(OutcomeCandle).where(
                OutcomeCandle.outcome_id == outcome_id,
                OutcomeCandle.interval == interval,
                OutcomeCandle.bucket_start >= datetime.fromtimestamp(segment_epoch, tz=timezone.utc),
                OutcomeCandle.bucket_start < datetime.fromtimestamp(seg_end, tz=timezone.utc),
            ).order_by(OutcomeCandle.bucket_start.asc())
        )).scalars().all()

    enc = _encode_db_rows(interval, segment_epoch, rows)
    _lru_put(key, enc)
    return _json_immutable(enc)
