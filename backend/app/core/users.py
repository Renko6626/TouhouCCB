from typing import Optional

from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordRequestForm
from fastapi_users import BaseUserManager, FastAPIUsers, IntegerIDMixin
from fastapi_users import exceptions
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from starlette import status
from starlette.exceptions import HTTPException

from fastapi_users.authentication import (
    AuthenticationBackend,
    BearerTransport,
    JWTStrategy,
)

from app.core.config import settings
from app.core.database import get_async_session
from app.models.base import User


# 1) Bearer 传输（注意加前导 /）
bearer_transport = BearerTransport(tokenUrl="/api/v1/auth/jwt/login")


# 2) JWT 策略
def get_jwt_strategy() -> JWTStrategy:
    return JWTStrategy(
        secret=settings.SECRET_KEY,
        lifetime_seconds=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


# 3) auth backend
auth_backend = AuthenticationBackend(
    name="jwt",
    transport=bearer_transport,
    get_strategy=get_jwt_strategy,
)


class UserManager(IntegerIDMixin, BaseUserManager[User, int]):
    reset_password_token_secret = settings.SECRET_KEY
    verification_token_secret = settings.SECRET_KEY

    async def authenticate(self, credentials: OAuth2PasswordRequestForm) -> Optional[User]:
        try:
            user = await self.get_by_email(credentials.username)
        except exceptions.UserNotExists:
            query = select(User).where(User.username == credentials.username)
            result = await self.user_db.session.execute(query)
            user = result.scalars().first()
            if user is None:
                # Run the hasher to mitigate timing attack
                self.password_helper.hash(credentials.password)
                return None

        verified, updated_password_hash = self.password_helper.verify_and_update(
            credentials.password, user.hashed_password
        )
        if not verified:
            return None

        if updated_password_hash is not None:
            await self.user_db.update(user, {"hashed_password": updated_password_hash})

        return user

    async def create(self, user_create, safe: bool = False, request: Optional[Request] = None):
        user_dict = user_create.model_dump(exclude_unset=True)

        # 用户名唯一性校验（友好报错）
        query = select(User).where(User.username == user_dict["username"])
        result = await self.user_db.session.execute(query)
        if result.scalars().first():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="USERNAME_ALREADY_EXISTS",
            )

        # ✅ safe=True 通常来自 register_router：这里决定“注册后状态”
        if safe:
            user_dict["is_superuser"] = False
            user_dict["is_verified"] = False

            # ✅ 激活码制：允许登录后激活，使用 is_verified 控制权限
            user_dict["is_active"] = True

            user_dict["cash"] = float(settings.INITIAL_BALANCE)
            user_dict["debt"] = 0.0

        password = user_dict.pop("password")
        user_dict["hashed_password"] = self.password_helper.hash(password)

        created_user = await self.user_db.create(user_dict)
        return created_user

    async def update(self, user_update, user, safe: bool = False, request: Optional[Request] = None):
        update_dict = user_update.model_dump(exclude_unset=True)

        # 用户名唯一性校验
        if "username" in update_dict and update_dict["username"] != user.username:
            query = select(User).where(
                User.username == update_dict["username"],
                User.id != user.id,
            )
            result = await self.user_db.session.execute(query)
            if result.scalars().first():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="USERNAME_ALREADY_TAKEN",
                )

        if safe:
            forbidden = ["is_superuser", "is_verified", "is_active", "cash", "debt"]
            for field in forbidden:
                update_dict.pop(field, None)

        if "password" in update_dict:
            password = update_dict.pop("password")
            update_dict["hashed_password"] = self.password_helper.hash(password)

        updated_user = await self.user_db.update(user, update_dict)
        return updated_user


async def get_user_db(session: AsyncSession = Depends(get_async_session)):
    # 建议把这个 import 放文件顶部也行；放这里也没问题
    from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase
    yield SQLAlchemyUserDatabase(session, User)


async def get_user_manager(user_db=Depends(get_user_db)):
    yield UserManager(user_db)


fastapi_users = FastAPIUsers[User, int](get_user_manager, [auth_backend])

# ✅ 新增：允许 inactive 用户（用于 /activate）
current_user = fastapi_users.current_user(active=False)

# ✅ 交易/普通权限：必须 active
current_active_user = fastapi_users.current_user(active=True)

# ✅ 管理员：必须 active + superuser
current_superuser = fastapi_users.current_user(active=True, superuser=True)
