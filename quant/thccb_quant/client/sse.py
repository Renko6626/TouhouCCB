"""SseClient: spec §4.3。起步不启用，预留给未来动量类策略。

实现要点（实际启用时）：
- httpx.AsyncClient.stream("GET", f"/api/v1/stream/market/{market_id}")
- aiter_lines 按 "data:" 前缀解析 JSON
- 25s 心跳监测，连续 2 个 ping 间隔无事件 → 重连
- 后端 1 小时强断，到点前主动重连
- 重连后用 last_seq 锚点请求增量（如后端支持）
"""
from typing import AsyncIterator, Any


class SseClient:
    """占位：当前不启用。"""

    def __init__(self, *_, **__):
        raise NotImplementedError(
            "SseClient skeleton — implement when first SSE-driven strategy lands"
        )

    async def subscribe(self, market_id: int) -> AsyncIterator[dict[str, Any]]:
        raise NotImplementedError
        yield  # pragma: no cover
