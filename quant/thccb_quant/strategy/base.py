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
