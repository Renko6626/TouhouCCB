"""
轻量 OIDC 客户端 — 通过 .well-known/openid-configuration 自动发现端点，
用 JWKS 验证 ID Token，完全不依赖 casdoor SDK。

用法：
    oidc = OIDCClient(issuer_url, client_id, client_secret)
    await oidc.ensure_ready()           # 拉取 .well-known + JWKS
    tokens  = await oidc.exchange_code(code, redirect_uri)
    claims  = oidc.verify_token(tokens["id_token"])  # 或 access_token
"""

import logging
import time
from typing import Any, Dict, Optional

import httpx
import jwt
from jwt import PyJWKClient, PyJWK

logger = logging.getLogger(__name__)

# JWKS 缓存刷新间隔（秒）
_JWKS_REFRESH_INTERVAL = 3600


class OIDCClient:
    """面向 Casdoor / 任意 OIDC Provider 的轻量客户端。"""

    def __init__(
        self,
        issuer_url: str,
        client_id: str,
        client_secret: str,
        *,
        audience: Optional[str] = None,
        http_timeout: float = 10.0,
    ):
        self.issuer_url = issuer_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.audience = audience or client_id
        self.http_timeout = http_timeout

        # 运行时填充
        self._well_known: Dict[str, Any] = {}
        self._jwk_client: Optional[PyJWKClient] = None
        self._jwks_fetched_at: float = 0.0
        self._ready = False

    # ----------------------------------------------------------
    # 初始化：拉取 .well-known 和 JWKS
    # ----------------------------------------------------------

    async def ensure_ready(self) -> None:
        """拉取 OIDC 发现文档和 JWKS（幂等，可重复调用）。"""
        if self._ready and (time.monotonic() - self._jwks_fetched_at < _JWKS_REFRESH_INTERVAL):
            return
        await self._fetch_well_known()
        self._init_jwk_client()
        self._ready = True

    async def _fetch_well_known(self) -> None:
        url = f"{self.issuer_url}/.well-known/openid-configuration"
        async with httpx.AsyncClient(timeout=self.http_timeout) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            self._well_known = resp.json()
        logger.info("OIDC discovery loaded from %s", url)

    def _init_jwk_client(self) -> None:
        jwks_uri = self._well_known.get("jwks_uri")
        if not jwks_uri:
            raise RuntimeError("OIDC discovery 中缺少 jwks_uri")
        self._jwk_client = PyJWKClient(jwks_uri, cache_keys=True, lifespan=_JWKS_REFRESH_INTERVAL)
        self._jwks_fetched_at = time.monotonic()
        logger.info("JWKS client initialized: %s", jwks_uri)

    # ----------------------------------------------------------
    # 属性：从 .well-known 读取端点
    # ----------------------------------------------------------

    @property
    def token_endpoint(self) -> str:
        ep = self._well_known.get("token_endpoint")
        if not ep:
            raise RuntimeError("OIDC discovery 中缺少 token_endpoint")
        return ep

    @property
    def userinfo_endpoint(self) -> Optional[str]:
        return self._well_known.get("userinfo_endpoint")

    # ----------------------------------------------------------
    # 核心：Code → Token 交换
    # ----------------------------------------------------------

    async def exchange_code(self, code: str, redirect_uri: str) -> Dict[str, Any]:
        """用 authorization code 换取 token 响应（含 access_token / id_token）。"""
        await self.ensure_ready()

        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": redirect_uri,
        }

        async with httpx.AsyncClient(timeout=self.http_timeout) as client:
            resp = await client.post(
                self.token_endpoint,
                data=payload,
                headers={"Accept": "application/json"},
            )

        if resp.status_code != 200:
            logger.error("Token exchange failed: %s %s", resp.status_code, resp.text[:500])
            raise RuntimeError(f"Token exchange failed ({resp.status_code})")

        return resp.json()

    # ----------------------------------------------------------
    # 核心：JWT 验证
    # ----------------------------------------------------------

    def verify_token(self, token: str) -> Dict[str, Any]:
        """
        用 JWKS 公钥验证并解码 JWT，返回 claims dict。
        支持 RS256 / ES256 等非对称算法。
        """
        if not self._jwk_client:
            raise RuntimeError("OIDC 客户端未初始化，请先调用 ensure_ready()")

        signing_key: PyJWK = self._jwk_client.get_signing_key_from_jwt(token)

        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=self.audience,
            options={
                # Casdoor 的 access_token 可能不含 aud，放宽验证
                "verify_aud": False,
            },
        )
        return claims
