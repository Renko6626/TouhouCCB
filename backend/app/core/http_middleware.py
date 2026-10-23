"""HTTP access policy and response-header logging."""
from __future__ import annotations

import logging
import time

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.services.credit import flags as credit_flags
from app.services.credit.ownership import OWNERSHIP, EconomicWritesDisabled

access_logger = logging.getLogger("thccb.access")

# 跳过高频或长连接路径，避免淹没日志：
# - /health 容器探活 10s/次
# - /api/v1/stream/* SSE，单连接可能持续一小时
# - /docs /redoc /openapi.json 仅开发调试用
_LOG_SKIP_PREFIXES = (
    "/health",
    "/api/v1/stream",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/favicon.ico",
    # SQLAdmin 自身的静态资源（/api/v1/admin/statics/*）和 UI 渲染请求噪声大，
    # 业务性动作（创建/结算/调整现金等）都走 /api/v1/market/* 和 /api/v1/user/*/adjust-cash
    # 等业务端点，会被正常记录，不依赖 admin 路径。
    "/api/v1/admin",
    # 阶段 4：不可变历史段，nginx 缓存回源为主，量大且无审计价值
    "/history/",
)



class AccessPolicyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope["method"]
        if method not in {"GET", "HEAD", "OPTIONS"}:
            if credit_flags.read_only_from_env() or OWNERSHIP.reason is not None:
                try:
                    OWNERSHIP.require_writes()
                except EconomicWritesDisabled as exc:
                    await JSONResponse(status_code=503, content={"detail": str(exc)})(scope, receive, send)
                    return

        path = scope["path"]
        log_request = not any(path.startswith(prefix) for prefix in _LOG_SKIP_PREFIXES)
        start = time.perf_counter()
        response_started = False

        async def send_response(message: Message) -> None:
            nonlocal response_started
            if message["type"] != "http.response.start":
                await send(message)
                return
            response_started = True
            if path.startswith("/api/v1/"):
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                headers["Pragma"] = "no-cache"
            if log_request:
                access_logger.log(
                    logging.WARNING if message["status"] >= 500 else logging.INFO,
                    "%s %s %d %.1fms", method, path, message["status"],
                    (time.perf_counter() - start) * 1000,
                )
            await send(message)

        try:
            await self.app(scope, receive, send_response)
        except Exception:
            if log_request and not response_started:
                access_logger.exception(
                    "%s %s EXC %.1fms", method, path,
                    (time.perf_counter() - start) * 1000,
                )
            raise
