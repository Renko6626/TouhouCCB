"""定频广播帧 broadcaster（spec § 5.1）。

writer 新路径与 market.py 老路径在每笔成交/状态变更/强平后 feed 本模块；
全局 8 Hz tick loop 扫 dirty 市场，把「价格向量 + 帧窗口内逐笔成交 + 市场
状态」打成一个 tick 帧经 BROKER.publish 发出——publish 已是序列化一次投
bytes（阶段 0），tick 帧天然继承。

迁移期兼容不变式（本文件最重要的约束）：tick 帧与老 trade/market_status
事件共用 BROKER 的同一个 per-market seq 计数器。quant bot 的 SSE 解析器对
未知事件类型也参与 gap 检测（quant/.../sse.py:74），共用计数器让 bot 收到
tick 帧时 seq 仍连续、内容被忽略——双发期间 bot 零改动不受影响。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from app.core.database import async_session_maker
from app.models.base import MarketStatus
from app.services import site_config

logger = logging.getLogger(__name__)


async def legacy_events_enabled() -> bool:
    """老 trade/market_status 事件双发开关（spec § 5.4，阶段 5 连代码一起删）。

    site_config 60 s 进程缓存：缓存命中时 async_session_maker() 上下文不执行
    任何 SQL、不 checkout 连接，可在每次 publish 前调用。
    """
    async with async_session_maker() as s:
        return await site_config.get_bool_or(s, "legacy_trade_events", True)


def _status_str(status: Any) -> str:
    """帧内 status 统一为 "trading"/"halt"/"settled" 小写值。

    writer 状态可能是 MarketStatus 枚举（op_close 等赋 new_status），也可能是
    DB 读回的裸 str；str(MarketStatus.X) 会得到 "MarketStatus.X"，必须取 .value。
    """
    if isinstance(status, MarketStatus):
        return status.value
    return str(status)


@dataclass
class _Pending:
    prices: list[float] = field(default_factory=list)
    status: str = MarketStatus.TRADING.value
    trades: list[dict] = field(default_factory=list)
    settlement: Optional[dict] = None
    dirty: bool = False


class TickBroadcaster:
    TICK_INTERVAL = 0.125   # 8 Hz（spec § 5.1）

    def __init__(self) -> None:
        self._pending: dict[int, _Pending] = {}
        self._task: asyncio.Task | None = None

    def _entry(self, market_id: int) -> _Pending:
        return self._pending.setdefault(int(market_id), _Pending())

    def feed_trade(self, market_id: int, prices: list[float], trade: dict, status: Any) -> None:
        p = self._entry(market_id)
        p.prices = list(prices)
        p.status = _status_str(status)
        p.trades.append(trade)
        p.dirty = True

    def feed_prices(self, market_id: int, prices: list[float], status: Any) -> None:
        p = self._entry(market_id)
        p.prices = list(prices)
        p.status = _status_str(status)
        p.dirty = True

    def feed_status(self, market_id: int, status: Any,
                    settlement: Optional[dict] = None,
                    prices: Optional[list[float]] = None) -> None:
        p = self._entry(market_id)
        p.status = _status_str(status)
        if settlement is not None:
            p.settlement = settlement
        if prices is not None:
            p.prices = list(prices)
        p.dirty = True

    async def flush_once(self) -> int:
        from app.services.realtime import BROKER   # 局部 import 避免环
        sent = 0
        for market_id, p in list(self._pending.items()):
            if not p.dirty:
                continue
            data: dict = {
                "status": p.status,
                "prices": list(p.prices),
                "trades": p.trades,
            }
            if p.settlement is not None:
                data["settlement"] = p.settlement
            # 先摘 pending 再 await publish：publish 期间新 feed 进下一帧
            p.trades = []
            p.settlement = None
            p.dirty = False
            await BROKER.publish(market_id, "tick", data)
            sent += 1
        return sent

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="tick-broadcaster")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        try:
            await self.flush_once()   # 停机前把残帧发出去
        except Exception:
            logger.exception("tick broadcaster final flush failed")
        self._pending.clear()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.TICK_INTERVAL)
            try:
                await self.flush_once()
            except Exception:
                logger.exception("tick broadcaster loop error")


TICK_BROADCASTER = TickBroadcaster()
