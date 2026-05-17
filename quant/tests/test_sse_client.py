"""SseClient: wire format 解析、gap detection、重连。

用 respx 不太方便 mock streaming，改用直接 mock httpx.AsyncClient.stream。
"""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from thccb_quant.client.auth import TokenManager
from thccb_quant.client.sse import SseClient, SseEvent


def _jwt(exp=3600):
    import base64, json
    p = {"exp": int(time.time()) + exp}
    return (
        base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
        + "." + base64.urlsafe_b64encode(json.dumps(p).encode()).rstrip(b"=").decode()
        + ".sig"
    )


class FakeStream:
    """模拟 httpx 流式响应。`lines` 是按顺序 yield 的文本行。"""

    def __init__(self, lines: list[str], status_code: int = 200):
        self._lines = lines
        self.status_code = status_code

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)


def _mk_client_with_streams(stream_factories):
    """stream_factories: 每次调 client.stream() 返回一个 FakeStream。

    iter 用完后再调会抛 ConnectError（用来终结测试循环）。
    """
    it = iter(stream_factories)
    client = MagicMock()

    @asynccontextmanager
    async def _stream(*args, **kwargs):
        try:
            s = next(it)
        except StopIteration:
            raise httpx.ConnectError("no more streams")
        yield s

    client.stream = _stream
    return client


async def _mk_sse(tmp_path: Path, http_client) -> SseClient:
    env = tmp_path / ".env"
    env.write_text("")
    raw = httpx.AsyncClient(base_url="http://x")
    mgr = TokenManager(base_url="http://x", access_token=_jwt(),
                       refresh_token=_jwt(86400), env_path=env, raw_client=raw)
    sse = SseClient(base_url="http://x", token_manager=mgr, raw_client=http_client)
    return sse


async def test_parses_snapshot_and_trade(tmp_path: Path):
    stream = FakeStream([
        "event: snapshot",
        'data: {"type":"snapshot","seq":0,"data":{"id":1,"outcomes":[]}}',
        "",
        "event: trade",
        'data: {"type":"trade","seq":1,"data":{"trade":{"id":99}}}',
        "",
    ])
    client = _mk_client_with_streams([stream])
    sse = await _mk_sse(tmp_path, client)
    got = []
    async def collect():
        async for ev in sse.subscribe(1):
            got.append(ev)
            if len(got) >= 2:
                return
    try:
        await asyncio.wait_for(collect(), timeout=2.0)
    except asyncio.TimeoutError:
        pass
    assert len(got) == 2
    assert got[0].type == "snapshot"
    assert got[0].seq == 0
    assert got[1].type == "trade"
    assert got[1].seq == 1
    assert got[1].data["trade"]["id"] == 99


async def test_gap_triggers_reconnect_rebootstrap(tmp_path: Path):
    """seq 跳跃 → 关连接重连，新 snapshot 重置 lastSeq。"""
    stream1 = FakeStream([
        "event: snapshot",
        'data: {"type":"snapshot","seq":10,"data":{}}',
        "",
        "event: trade",
        'data: {"type":"trade","seq":11,"data":{"trade":{"id":1}}}',
        "",
        "event: trade",
        'data: {"type":"trade","seq":15,"data":{"trade":{"id":2}}}',  # gap
        "",
    ])
    stream2 = FakeStream([
        "event: snapshot",
        'data: {"type":"snapshot","seq":20,"data":{}}',
        "",
    ])
    client = _mk_client_with_streams([stream1, stream2])
    sse = await _mk_sse(tmp_path, client)
    got = []
    async def collect():
        async for ev in sse.subscribe(1):
            got.append(ev)
            if len(got) >= 4:
                return
    try:
        await asyncio.wait_for(collect(), timeout=2.0)
    except asyncio.TimeoutError:
        pass
    # 期望：snap(10)、trade(11)、（trade(15) 触发 gap，不 yield）→ snap(20) 带 gap_recover
    types_seqs = [(ev.type, ev.seq) for ev in got]
    assert ("snapshot", 10) in types_seqs
    assert ("trade", 11) in types_seqs
    assert ("snapshot", 20) in types_seqs
    snap20 = next(ev for ev in got if ev.type == "snapshot" and ev.seq == 20)
    assert snap20.data.get("gap_recover") is True
