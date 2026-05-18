"""Runtime registry —— web UI 用的进程内单例。

WebUI 直接读 trader 进程的活体状态（策略 instance / SSE subscriber / Store），
所以这个 module 只是个数据 holder，不引入依赖。trader.py main_async 启动时
填字段，web.py 启动时挂上来读。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from thccb_quant.client.sse_subscriber import SseSubscriber
    from thccb_quant.state.store import Store
    from thccb_quant.strategy.base import Strategy


@dataclass
class Runtime:
    started_at: float = field(default_factory=time.monotonic)
    started_at_wall: float = field(default_factory=time.time)
    dry_run: bool = False
    base_url: str = ""
    strategies: list["Strategy"] = field(default_factory=list)
    subscriber: Optional["SseSubscriber"] = None
    store: Optional["Store"] = None
    config: dict = field(default_factory=dict)


RUNTIME = Runtime()
