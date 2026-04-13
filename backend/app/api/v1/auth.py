"""
认证路由 — Casdoor OAuth2 回调 + 当前用户信息。

POST /api/v1/auth/callback   前端把 Casdoor 返回的 code 发过来，换取本站 JWT
GET  /api/v1/auth/me         返回当前登录用户信息
"""

import asyncio
from typing import Optional

from casdoor import CasdoorSDK
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from decimal import Decimal

from pydantic import BaseModel

from app.core.config import settings
from app.core.database import get_async_session, managed_transaction
from app.core.users import create_access_token, create_refresh_token, verify_refresh_token, current_active_user
from app.models.base import User

router = APIRouter()

# Casdoor SDK 实例（在模块加载时初始化，配置从环境变量读取）
_casdoor = CasdoorSDK(
    endpoint=settings.CASDOOR_ENDPOINT,
    client_id=settings.CASDOOR_CLIENT_ID,
    client_secret=settings.CASDOOR_CLIENT_SECRET,
    certificate=settings.CASDOOR_CERTIFICATE,
    org_name=settings.CASDOOR_ORG_NAME,
    app_name=settings.CASDOOR_APP_NAME,
)


@router.post("/callback", summary="Casdoor OAuth2 回调 — 换取本站 JWT")
async def oauth_callback(
    code: str,
    state: Optional[str] = None,
    db: AsyncSession = Depends(get_async_session),
):
    """
    前端在 /auth/callback 页面拿到 Casdoor 返回的 code，POST 到此接口。
    1. 用 code 向 Casdoor 换 access_token
    2. 解析 JWT，取出用户身份
    3. 查找或创建本站用户记录（首次登录自动开户）
    4. 颁发本站 JWT
    """
    # SDK 调用是同步阻塞的，放到线程池避免阻塞事件循环
    import logging
    _logger = logging.getLogger(__name__)
    try:
        token = await asyncio.to_thread(_casdoor.get_oauth_token, code)
        claims = await asyncio.to_thread(_casdoor.parse_jwt_token, token.access_token)
    except Exception as e:
        _logger.error(f"Casdoor 认证失败: {type(e).__name__}: {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="认证失败，请重试")

    casdoor_id: str = claims["sub"]
    username: str = claims.get("name") or claims.get("preferred_username") or casdoor_id
    email: Optional[str] = claims.get("email") or None

    # 查找或创建本站用户
    result = await db.execute(select(User).where(User.casdoor_id == casdoor_id))
    user: Optional[User] = result.scalars().first()

    if not user:
        async with managed_transaction(db):
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
                is_superuser=False,
            )
            db.add(user)
            await db.flush()   # 获取 user.id（在 commit 之前）
    elif not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号已被禁用")

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
