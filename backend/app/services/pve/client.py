"""回环 HTTP 下单客户端（spec §3 / §8）。

机器人不走登录：进程内直接用 core.users.create_access_token 给机器人 user 签
短期 JWT；活动模式的 anti-bot L2 用 CLIENT_TOKEN_SECRET 自算 HMAC client token。
请求直连本机 uvicorn（默认 http://127.0.0.1:8004，可用 PVE_SELF_BASE_URL 覆盖），
绕过 nginx 限速——该层保护由 engine 的全局每分钟上限 + 串行下单接管。

测试可传 httpx transport（ASGITransport(test_app)）替代真实回环。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from decimal import Decimal
from typing import Optional

import httpx

from app.core.config import settings
from app.core.users import create_access_token

_API = "/api/v1/market"


class PveTradeError(Exception):
    """下单/报价被拒（HTTP >= 400）。detail 进决策日志。"""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"HTTP {status_code}: {detail}")


class LoopbackTrader:
    def __init__(
        self,
        base_url: Optional[str] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self._base_url = base_url or os.getenv("PVE_SELF_BASE_URL", "http://127.0.0.1:8004")
        self._transport = transport
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            if self._transport is not None:
                self._client = httpx.AsyncClient(
                    transport=self._transport, base_url="http://pve.internal", timeout=10.0
                )
            else:
                self._client = httpx.AsyncClient(base_url=self._base_url, timeout=10.0)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self, user_id: int) -> dict:
        headers = {"Authorization": f"Bearer {create_access_token(user_id)}"}
        secret = settings.CLIENT_TOKEN_SECRET
        if secret:
            ts = str(int(time.time()))
            token = hmac.new(
                secret.encode(), f"{ts}|{user_id}".encode(), hashlib.sha256
            ).hexdigest()
            headers["X-Client-Token"] = token
            headers["X-Client-TS"] = ts
        return headers

    async def _post(self, path: str, user_id: int, payload: dict) -> dict:
        resp = await self._get_client().post(
            f"{_API}{path}", json=payload, headers=self._headers(user_id)
        )
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            raise PveTradeError(resp.status_code, str(detail)[:300])
        return resp.json()

    async def quote(self, user_id: int, outcome_id: int, shares: Decimal, side: str) -> dict:
        return await self._post(
            "/quote", user_id,
            {"outcome_id": outcome_id, "shares": str(shares), "side": side},
        )

    # accept_any_slippage：market API 会把 max_slippage_bps 截到
    # trade_checks.HARDCAP_SLIPPAGE_BPS=1000（10%），所以站点配置 pve_max_slippage_bps
    # 调到 1000 以上时，单子会先过引擎自检、再被 API 拒（白跑一趟且记成 error）。
    # 引擎在 _execute 里已按 pve_max_slippage_bps 自己算过滑点并拦截，这里声明
    # 「已明确接受」把裁决权收归引擎一处，避免两道口径打架。
    async def buy(
        self, user_id: int, outcome_id: int, shares: Decimal, max_slippage_bps: int,
        accept_any_slippage: bool = True,
    ) -> dict:
        return await self._post(
            "/buy", user_id,
            {"outcome_id": outcome_id, "shares": str(shares),
             "max_slippage_bps": min(max_slippage_bps, 10000),
             "accept_any_slippage": accept_any_slippage},
        )

    async def sell(
        self, user_id: int, outcome_id: int, shares: Decimal, max_slippage_bps: int,
        accept_any_slippage: bool = True,
    ) -> dict:
        return await self._post(
            "/sell", user_id,
            {"outcome_id": outcome_id, "shares": str(shares),
             "max_slippage_bps": min(max_slippage_bps, 10000),
             "accept_any_slippage": accept_any_slippage},
        )
