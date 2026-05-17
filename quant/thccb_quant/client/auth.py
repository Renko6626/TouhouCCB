"""TokenManager: JWT 解析、自动 refresh、写回 .env。spec §4.1。

后端 /auth/refresh 只返回新 access_token，不轮换 refresh_token，所以这里
不更新 refresh。
"""
import asyncio
import base64
import json
import time
from pathlib import Path

import httpx
from dotenv import set_key

from thccb_quant.errors import FatalAuthError


def jwt_decode_exp(token: str) -> int:
    """从 JWT payload 取 exp（不验签）。"""
    parts = token.split(".")
    if len(parts) < 2:
        raise ValueError("invalid jwt")
    payload_b64 = parts[1]
    # padding
    payload_b64 += "=" * (-len(payload_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    return int(payload["exp"])


class TokenManager:
    REFRESH_BUFFER_SEC = 300  # 剩 < 5 min 触发刷新

    def __init__(
        self,
        *,
        base_url: str,
        access_token: str,
        refresh_token: str,
        env_path: Path,
        raw_client: httpx.AsyncClient,
    ):
        self._base_url = base_url
        self._access = access_token
        self._refresh = refresh_token
        self._env_path = env_path
        self._client = raw_client
        self._exp = jwt_decode_exp(access_token)
        self._refresh_exp = jwt_decode_exp(refresh_token)
        self._lock = asyncio.Lock()

    @property
    def refresh_exp_ts(self) -> int:
        return self._refresh_exp

    async def get_valid_access(self) -> str:
        if self._exp - time.time() < self.REFRESH_BUFFER_SEC:
            async with self._lock:
                if self._exp - time.time() < self.REFRESH_BUFFER_SEC:
                    await self._refresh_token()
        return self._access

    async def _refresh_token(self) -> None:
        resp = await self._client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": self._refresh},
        )
        if resp.status_code != 200:
            raise FatalAuthError(
                f"refresh failed: {resp.status_code} {resp.text[:200]}"
            )
        data = resp.json()
        self._access = data["access_token"]
        self._exp = jwt_decode_exp(self._access)
        set_key(str(self._env_path), "THCCB_ACCESS_TOKEN", self._access)
