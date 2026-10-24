"""Deny business HTTP requests during an administrator-controlled site closure."""
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.routing import Match, Mount

from app.core.database import async_session_maker
from app.core.users import user_from_access_token
from app.services import returned_config
from app.services.site_access import is_disabled

# Authentication must still work when an administrator has logged out or expired.
_PUBLIC_PATHS = frozenset({
    "/api/v1/site/access",
    "/api/v1/auth/login-start", "/api/v1/auth/callback", "/api/v1/auth/refresh",
    "/api/v1/auth/me", "/api/v1/auth/dev-login",
    "/api/v1/admin/login", "/api/v1/admin/logout",
})


class SiteAccessMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"].rstrip("/")
        if (scope["method"] == "OPTIONS" or not path.startswith(("/api/", "/history/"))
                or path in _PUBLIC_PATHS or path.startswith("/api/v1/admin/statics/")):
            await self.app(scope, receive, send)
            return

        # SQLAdmin owns its signed session inside a mounted app. Delegate only
        # requests routed to that mount to its existing administrator authentication.
        if path == "/api/v1/admin" or path.startswith("/api/v1/admin/"):
            for route in scope["app"].router.routes:
                if route.matches(scope)[0] == Match.FULL:
                    if isinstance(route, Mount) and route.path == "/api/v1/admin":
                        await self.app(scope, receive, send)
                        return
                    break

        response = None
        async with async_session_maker() as db:
            if await is_disabled(db):
                request = Request(scope)
                authorization = request.headers.get("authorization", "")
                scheme, _, credential = authorization.partition(" ")
                source_allowed = False
                if path.startswith("/api/v1/returned/") and scheme.lower() == "bearer":
                    config = await returned_config.read_config(db)
                    source_allowed = config["enabled"] and returned_config.matches_secret(credential, config)
                user = None
                if not source_allowed and scheme.lower() == "bearer":
                    try:
                        user = await user_from_access_token(authorization[7:].strip(), db)
                    except HTTPException as exc:
                        # Keep 401 renewal working for administrators with an expired token.
                        response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
                if response is None and not source_allowed and not (user and user.is_active and user.is_superuser):
                    response = JSONResponse({"detail": "SITE_ACCESS_DISABLED"}, status_code=403)
        if response is not None:
            response.headers["Cache-Control"] = "no-store"
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
