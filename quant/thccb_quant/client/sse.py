"""SseClient: 长连接 SSE，单 market 一个实例。spec §5。

自动处理：wire 解析 / 25s 心跳超时 / 58 min 主动重连 / seq gap detection
→ 重连重 bootstrap / 网络错误指数退避 0.5/1/2/5/10s 封顶。
"""
import asyncio
import json
import time
from dataclasses import dataclass
from typing import AsyncIterator, Literal

import httpx
import structlog

from thccb_quant.client.auth import TokenManager

_log = structlog.get_logger("sse_client")

ZOMBIE_TIMEOUT_SEC = 25.0
PREEMPTIVE_RECONNECT_SEC = 58 * 60
BACKOFF_SECS = [0.5, 1.0, 2.0, 5.0, 10.0]


@dataclass
class SseEvent:
    type: Literal["snapshot", "trade", "market_status", "ping"]
    seq: int
    data: dict


class SseClient:
    def __init__(
        self,
        *,
        base_url: str,
        token_manager: TokenManager,
        raw_client: httpx.AsyncClient | None = None,
    ):
        self._base_url = base_url
        self._tm = token_manager
        self._client = raw_client or httpx.AsyncClient(base_url=base_url, timeout=None)

    async def subscribe(self, market_id: int) -> AsyncIterator[SseEvent]:
        backoff_idx = 0
        last_seq = -1
        next_is_gap_recover = False
        while True:
            try:
                token = await self._tm.get_valid_access()
                headers = {"Authorization": f"Bearer {token}",
                           "Accept": "text/event-stream"}
                connect_at = time.monotonic()
                async with self._client.stream(
                    "GET", f"/api/v1/stream/market/{market_id}",
                    headers=headers,
                ) as resp:
                    resp.raise_for_status()
                    backoff_idx = 0
                    async for event in _parse_sse_stream(resp):
                        if event.type == "snapshot":
                            if next_is_gap_recover:
                                event.data["gap_recover"] = True
                                next_is_gap_recover = False
                            last_seq = event.seq
                            yield event
                        elif event.type == "ping":
                            pass  # 不 yield，仅靠 parse 内的 timeout 重置
                        else:
                            if event.seq == last_seq + 1:
                                last_seq = event.seq
                                yield event
                            else:
                                _log.warning("sse_gap_reconnect",
                                             market_id=market_id,
                                             expected=last_seq + 1,
                                             got=event.seq)
                                next_is_gap_recover = True
                                last_seq = -1
                                break
                        if time.monotonic() - connect_at > PREEMPTIVE_RECONNECT_SEC:
                            _log.info("sse_preemptive_reconnect", market_id=market_id)
                            break
            except httpx.TimeoutException:
                _log.info("sse_timeout_reconnect", market_id=market_id)
                next_is_gap_recover = True
                last_seq = -1
            except httpx.HTTPError as e:
                _log.error("sse_http_error", market_id=market_id, error=str(e))
                await asyncio.sleep(BACKOFF_SECS[min(backoff_idx, len(BACKOFF_SECS) - 1)])
                backoff_idx += 1
                next_is_gap_recover = True
                last_seq = -1
                continue
            except Exception as e:
                _log.exception("sse_unknown_error", market_id=market_id, error=str(e))
                await asyncio.sleep(BACKOFF_SECS[min(backoff_idx, len(BACKOFF_SECS) - 1)])
                backoff_idx += 1
                next_is_gap_recover = True
                last_seq = -1
                continue


async def _parse_sse_stream(resp) -> AsyncIterator[SseEvent]:
    """SSE wire format 子集解析。

    后端只用 `event:` 和 `data:` 两行 + 空行分隔块。
    25s 无任何行视为 zombie，抛 TimeoutException 让上层重连。
    """
    cur_event_type = None
    cur_data = None

    try:
        async for line in _aiter_lines_with_timeout(resp, ZOMBIE_TIMEOUT_SEC):
            line = line.rstrip("\r")
            if not line:
                # 块结束，emit 一个 event
                if cur_event_type and cur_data is not None:
                    try:
                        payload = json.loads(cur_data)
                    except json.JSONDecodeError:
                        _log.warning("sse_parse_failed", line=cur_data[:100])
                        cur_event_type = cur_data = None
                        continue
                    yield SseEvent(
                        type=payload.get("type", cur_event_type),
                        seq=int(payload.get("seq", 0)),
                        data=payload.get("data", {}),
                    )
                cur_event_type = cur_data = None
                continue
            if line.startswith(":"):
                continue  # comment
            if line.startswith("event:"):
                cur_event_type = line[6:].strip()
            elif line.startswith("data:"):
                cur_data = line[5:].strip()
    except asyncio.TimeoutError:
        raise httpx.TimeoutException("sse zombie timeout")


async def _aiter_lines_with_timeout(resp, timeout: float):
    """asyncio.wait_for 包 aiter_lines；timeout 内无新行抛 TimeoutError；
    上游 EOF 时正常 return（避免 async gen 把 StopAsyncIteration 变成 RuntimeError）。
    """
    it = resp.aiter_lines().__aiter__()
    while True:
        try:
            line = await asyncio.wait_for(it.__anext__(), timeout=timeout)
        except StopAsyncIteration:
            return
        yield line
