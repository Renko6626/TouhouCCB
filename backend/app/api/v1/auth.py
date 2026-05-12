"""
认证路由 — Casdoor OIDC 回调 + 当前用户信息。

POST /api/v1/auth/callback   前端把 Casdoor 返回的 code 发过来，换取本站 JWT
POST /api/v1/auth/refresh    用 refresh token 换取新的 access token
GET  /api/v1/auth/me         返回当前登录用户信息

不依赖 casdoor SDK，通过 .well-known/openid-configuration 自动发现端点。
"""

import logging
import secrets
from typing import Optional
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_async_session, managed_transaction
from app.core.oidc import OIDCClient
from app.core.users import create_access_token, create_refresh_token, verify_refresh_token, current_active_user
from app.models.base import User

logger = logging.getLogger(__name__)

router = APIRouter()

# OAuth state/nonce cookie names + lifetime
_STATE_COOKIE = "thccb_oauth_state"
_NONCE_COOKIE = "thccb_oauth_nonce"
_COOKIE_MAX_AGE = 300  # 5 分钟，足以走完 Casdoor 登录页
_COOKIE_PATH = "/api/v1/auth"

# OIDC 客户端实例（延迟初始化：首次请求时拉取 .well-known）
_oidc: Optional[OIDCClient] = None


def _get_oidc() -> OIDCClient:
    """获取 OIDC 客户端单例。Casdoor 未配置时拒绝请求。"""
    global _oidc
    if _oidc is None:
        if not settings.CASDOOR_ENDPOINT or not settings.CASDOOR_CLIENT_ID:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Casdoor SSO 未配置，无法使用 OAuth 登录",
            )
        _oidc = OIDCClient(
            issuer_url=settings.CASDOOR_ENDPOINT,
            client_id=settings.CASDOOR_CLIENT_ID,
            client_secret=settings.CASDOOR_CLIENT_SECRET,
        )
    return _oidc


class LoginStartResponse(BaseModel):
    state: str
    nonce: str


@router.post("/login-start", response_model=LoginStartResponse, summary="生成 OAuth state/nonce 并写 HttpOnly cookie")
async def login_start(response: Response):
    """
    前端跳转到 Casdoor 之前必须先调此接口。
    服务端生成随机 state + nonce，写入 HttpOnly cookie，并把同值返回给前端拼到
    Casdoor authorize URL 里。/callback 时服务端从 cookie 读取并比对。
    """
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)

    cookie_kwargs = dict(
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        secure=settings.FRONTEND_URL.startswith("https://"),
        samesite="lax",
        path=_COOKIE_PATH,
    )
    response.set_cookie(_STATE_COOKIE, state, **cookie_kwargs)
    response.set_cookie(_NONCE_COOKIE, nonce, **cookie_kwargs)

    return LoginStartResponse(state=state, nonce=nonce)


class CallbackRequest(BaseModel):
    code: str
    state: str


@router.post("/callback", summary="Casdoor OAuth2 回调 — 换取本站 JWT")
async def oauth_callback(
    body: CallbackRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_async_session),
):
    """
    前端在 /auth/callback 页面拿到 Casdoor 返回的 code，POST 到此接口。
    1. 校验 state：cookie 中存的 state 必须等于 body 提交的 state（CSRF / login fixation 防护）
    2. 用 code + 服务端硬编码 redirect_uri 向 Casdoor 换 token
    3. 用 JWKS 验证 id_token 签名 + iss + aud + nonce
    4. 查找或创建本站用户记录（首次登录自动开户）
    5. 颁发本站 JWT，清掉 state/nonce cookie
    """
    # 1. 校验 state（防 CSRF / login fixation）
    cookie_state = request.cookies.get(_STATE_COOKIE)
    cookie_nonce = request.cookies.get(_NONCE_COOKIE)

    if not cookie_state or not secrets.compare_digest(cookie_state, body.state):
        response.delete_cookie(_STATE_COOKIE, path=_COOKIE_PATH)
        response.delete_cookie(_NONCE_COOKIE, path=_COOKIE_PATH)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="state 校验失败，请重新登录",
        )

    if not cookie_nonce:
        response.delete_cookie(_STATE_COOKIE, path=_COOKIE_PATH)
        response.delete_cookie(_NONCE_COOKIE, path=_COOKIE_PATH)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="nonce 缺失，请重新登录",
        )

    oidc = _get_oidc()

    # redirect_uri 服务端硬编码，不接受客户端提交（防开放重定向链）
    redirect_uri = f"{settings.FRONTEND_URL}/auth/callback"

    try:
        await oidc.ensure_ready()
        token_resp = await oidc.exchange_code(body.code, redirect_uri)
    except Exception as e:
        logger.error("OIDC token exchange failed: %s: %s", type(e).__name__, e)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="认证失败，请重试")

    # 必须用 id_token（含 nonce），不再 fallback 到 access_token
    raw_token = token_resp.get("id_token")
    if not raw_token:
        logger.error("Token response missing id_token")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="认证服务返回异常")

    try:
        claims = oidc.verify_token(raw_token, nonce=cookie_nonce)
    except Exception as e:
        logger.error("JWT verification failed: %s: %s", type(e).__name__, e)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="令牌验证失败，请重试")

    casdoor_id: str = claims.get("sub", "")
    if not casdoor_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="令牌中缺少用户标识")

    username: str = claims.get("name") or claims.get("preferred_username") or casdoor_id
    email: Optional[str] = claims.get("email") or None

    # 查找或创建本站用户
    result = await db.execute(select(User).where(User.casdoor_id == casdoor_id))
    user: Optional[User] = result.scalars().first()

    if not user:
        async with managed_transaction(db):
            # 第一个注册的用户自动成为管理员
            user_count = await db.execute(select(func.count()).select_from(User))
            is_first_user = user_count.scalar_one() == 0

            # 用户名可能与已有账号冲突（极低概率），加后缀保证唯一
            base = username
            suffix = 0
            while True:
                name_check = await db.execute(select(User).where(User.username == username))
                if not name_check.scalars().first():
                    break
                suffix += 1
                username = f"{base}_{suffix}"

            user = User(
                casdoor_id=casdoor_id,
                username=username,
                email=email,
                cash=Decimal(str(settings.INITIAL_BALANCE)),
                debt=Decimal("0"),
                is_active=True,
                is_superuser=is_first_user,
            )
            db.add(user)
            await db.flush()

            if is_first_user:
                logger.info("First user registered as admin: %s (id=%s)", username, user.id)
    elif not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号已被禁用")

    # 校验成功，清掉一次性 cookie（防被重放）
    response.delete_cookie(_STATE_COOKIE, path=_COOKIE_PATH)
    response.delete_cookie(_NONCE_COOKIE, path=_COOKIE_PATH)

    return {
        "access_token": create_access_token(user.id),
        "refresh_token": create_refresh_token(user.id),
        "token_type": "bearer",
    }


class RefreshRequest(BaseModel):
    refresh_token: str


@router.post("/refresh", summary="用 refresh token 换取新的 access token")
async def refresh_access_token(
    body: RefreshRequest,
    db: AsyncSession = Depends(get_async_session),
):
    user_id = verify_refresh_token(body.refresh_token)
    user = await db.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在或已禁用")
    return {
        "access_token": create_access_token(user.id),
        "token_type": "bearer",
    }


@router.get("/me", summary="获取当前登录用户信息")
async def get_me(user: User = Depends(current_active_user)):
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "is_superuser": user.is_superuser,
        "is_active": user.is_active,
        "cash": user.cash,
        "debt": user.debt,
    }
