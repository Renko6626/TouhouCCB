"""Broker ABC：spec §5。"""
from abc import ABC, abstractmethod
from decimal import Decimal

from thccb_quant.client.rest import OrderResponse


class Broker(ABC):
    @abstractmethod
    async def buy(
        self, *, strategy: str, outcome_id: int, shares: Decimal, max_slippage_bps: int
    ) -> OrderResponse: ...

    @abstractmethod
    async def sell(
        self, *, strategy: str, outcome_id: int, shares: Decimal, max_slippage_bps: int
    ) -> OrderResponse: ...
