"""RestClient: 封装所有 thccb API 端点 + 自限速 + 错误分类。spec §4.2。"""
import asyncio
import time
from decimal import Decimal
from typing import Any, List, Literal, Optional

import httpx
from pydantic import BaseModel, ConfigDict

from thccb_quant.client.auth import TokenManager
from thccb_quant.errors import BusinessError, TransientError


# ── Response models ────────────────────────────────────────────

class OutcomePriceItem(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    label: str
    shares: float
    current_price: float


class QuoteResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    outcome_id: int
    side: str
    shares: Decimal
    avg_price: Decimal
    gross: Decimal
    fee: Decimal
    net: Decimal
    after_prices: List[OutcomePriceItem] = []


class OrderResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    shares: Decimal
    cost: Decimal
    new_cash: Decimal
    message: Optional[str] = None


class OutcomeDetail(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    label: str
    total_shares: Decimal
    current_price: Decimal


class MarketDetail(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    title: str
    status: str
    liquidity_b: float
    outcomes: List[OutcomeDetail]


class MarketSummary(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    title: str
    status: str


class HoldingRead(BaseModel):
    model_config = ConfigDict(extra="allow")
    outcome_id: int
    outcome_label: str
    market_id: int
    market_title: str
    amount: Decimal
    cost_basis: Decimal
    avg_price: Decimal
    current_price: Decimal
    market_value: Decimal
    unrealized_pnl: Decimal


class UserSummary(BaseModel):
    model_config = ConfigDict(extra="allow")
    cash: Decimal
    debt: Decimal
    holdings_value: Decimal
    net_worth: Decimal


class PartialTrade(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    outcome_id: int
    type: str
    shares: Decimal
    price: Decimal
    username: Optional[str] = None
    timestamp: str
    market_id: int
    market_title: Optional[str] = None
    outcome_label: Optional[str] = None


# ── RestClient ──────────────────────────────────────────────────

class _TokenBucket:
    """简易 token bucket：每秒补 rate 个 token，capacity = rate。"""

    def __init__(self, rate: float):
        self._rate = rate
        self._tokens = rate
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self._rate, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens < 1:
                wait = (1 - self._tokens) / self._rate
                await asyncio.sleep(wait)
                # 推进 _last 把刚 sleep 的时间记账掉，否则下次 acquire 会把 sleep 时长
                # 当成空闲补成 free token，节流就废了
                self._last = time.monotonic()
                self._tokens = 0
            else:
                self._tokens -= 1


class RestClient:
    MAX_RETRIES = 3
    BACKOFF_BASE = 0.2

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        token_manager: TokenManager,
        rate_limit_per_sec: float = 8.0,
    ):
        self._c = client
        self._tm = token_manager
        self._bucket = _TokenBucket(rate_limit_per_sec)

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        await self._bucket.acquire()
        token = await self._tm.get_valid_access()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"

        last_exc: Optional[Exception] = None
        for attempt in range(self.MAX_RETRIES):
            try:
                resp = await self._c.request(method, path, headers=headers, **kwargs)
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                last_exc = e
                await asyncio.sleep(self.BACKOFF_BASE * (2 ** attempt))
                continue

            if resp.status_code < 400:
                return resp
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                try:
                    detail = resp.json().get("detail", resp.text)
                except Exception:
                    detail = resp.text
                raise BusinessError(resp.status_code, str(detail))
            # 5xx / 429 → retry
            last_exc = TransientError(f"{resp.status_code} {resp.text[:200]}")
            await asyncio.sleep(self.BACKOFF_BASE * (2 ** attempt))

        if last_exc:
            raise TransientError(str(last_exc))
        raise TransientError("unknown")

    # ── Market endpoints ──

    async def quote(
        self, *, outcome_id: int, shares: Decimal, side: Literal["buy", "sell"]
    ) -> QuoteResponse:
        r = await self._request(
            "POST", "/api/v1/market/quote",
            json={"outcome_id": outcome_id, "shares": str(shares), "side": side},
        )
        return QuoteResponse.model_validate(r.json())

    async def buy(
        self, *, outcome_id: int, shares: Decimal, max_slippage_bps: int
    ) -> OrderResponse:
        r = await self._request(
            "POST", "/api/v1/market/buy",
            json={
                "outcome_id": outcome_id,
                "shares": str(shares),
                "max_slippage_bps": max_slippage_bps,
                "accept_any_slippage": False,
            },
        )
        return OrderResponse.model_validate(r.json())

    async def sell(
        self, *, outcome_id: int, shares: Decimal, max_slippage_bps: int
    ) -> OrderResponse:
        r = await self._request(
            "POST", "/api/v1/market/sell",
            json={
                "outcome_id": outcome_id,
                "shares": str(shares),
                "max_slippage_bps": max_slippage_bps,
                "accept_any_slippage": False,
            },
        )
        return OrderResponse.model_validate(r.json())

    async def list_markets(self) -> List[MarketSummary]:
        r = await self._request("GET", "/api/v1/market/list")
        return [MarketSummary.model_validate(x) for x in r.json()]

    async def get_market(self, market_id: int) -> MarketDetail:
        r = await self._request("GET", f"/api/v1/market/{market_id}")
        return MarketDetail.model_validate(r.json())

    # ── User endpoints ──

    async def get_holdings(self) -> List[HoldingRead]:
        r = await self._request("GET", "/api/v1/user/holdings")
        return [HoldingRead.model_validate(x) for x in r.json()]

    async def get_user_summary(self) -> UserSummary:
        r = await self._request("GET", "/api/v1/user/summary")
        return UserSummary.model_validate(r.json())

    async def get_recent_trades(self, *, limit: int = 100) -> List[PartialTrade]:
        r = await self._request(
            "GET", "/api/v1/market/recent-trades",
            params={"limit": limit},
        )
        return [PartialTrade.model_validate(x) for x in r.json()]
