"""Strategy ABC + StrategyContext。spec §6.1。"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import structlog

from thccb_quant.broker.base import Broker
from thccb_quant.client.rest import RestClient
from thccb_quant.state.store import Store


@dataclass
class StrategyContext:
    rest: RestClient
    broker: Broker
    store: Store
    logger: structlog.BoundLogger
    config: dict


class Strategy(ABC):
    name: str
    tick_interval_sec: int = 30
    market_id: int | None = None  # 由策略 setup() 设置；SseSubscriber 用以路由

    def __init__(self, name: str, config: dict):
        self.name = name
        self._config = config

    @abstractmethod
    async def setup(self, ctx: StrategyContext) -> None: ...

    @abstractmethod
    async def tick(self) -> None: ...

    async def on_sse_event(self, event: Any) -> None:
        """默认 no-op，需要实时反应的策略覆盖。"""

    async def teardown(self) -> None:
        """优雅停机钩子，默认 no-op。"""

    def snapshot(self) -> dict:
        """供 WebUI 读策略活体状态。默认实现只返回基本身份信息，
        各策略覆盖加自己的内部状态（EMA / 持仓 / 累计花费等）。"""
        return {
            "name": self.name,
            "type": self.__class__.__name__,
            "market_id": self.market_id,
            "tick_interval_sec": self.tick_interval_sec,
        }
