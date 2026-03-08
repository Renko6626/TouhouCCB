# app/api/v1/users.py

import secrets
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.users import auth_backend, fastapi_users, current_user, current_superuser
from app.schemas.auth import ActivateRequest, CreateActivationCodesRequest, ActivationCodeRead
from app.schemas.user import UserRead, UserCreate, UserUpdate
from app.models.base import User
from app.models.activation import ActivationCode  # 你需要按上面新增这个模型


router = APIRouter()


# -------------------------
# fastapi-users: auth/register/users
# -------------------------

router.include_router(
    fastapi_users.get_auth_router(auth_backend),
    prefix="/jwt",
)

router.include_router(
    fastapi_users.get_register_router(UserRead, UserCreate),
)

router.include_router(
    fastapi_users.get_users_router(UserRead, UserUpdate),
)


# =========================================================
# 激活码：用户激活
# =========================================================

@router.post("/activate", summary="使用激活码激活账号")
async def activate_account(
    req: ActivateRequest,
    user: User = Depends(current_user),  # 这里“active”是否合适见下方说明
    db: AsyncSession = Depends(get_async_session),
):
    """
    使用一次性激活码激活当前用户：
    - 消耗激活码（置 is_used=True, 记录 used_by_user_id/used_at）
    - 将用户置为 is_active=True, is_verified=True（你可以只开一个）
    """

    # ⚠️ 如果你希望“未激活用户也能登录来激活”，那 current_active_user 不合适。
    # 更合理是用 fastapi-users 的 current_user(active=False) 依赖。
    # 但你现在 core.users 里暴露的依赖我不知道是否有这个。
    # 先按你已有的 current_active_user 写；下面我也给你“更推荐”的写法说明。

    async with db.begin():
        # 1) 锁定激活码行，避免并发重复消费（MySQL/PG 有效）
        code_res = await db.execute(
            select(ActivationCode)
            .where(ActivationCode.code == req.code)
            .with_for_update()
        )
        act = code_res.scalars().first()
        if not act:
            raise HTTPException(status_code=404, detail="激活码不存在")
        if act.is_used:
            raise HTTPException(status_code=400, detail="激活码已被使用")

        # 2) 锁定用户行（避免并发激活/并发修改）
        user_res = await db.execute(
            select(User).where(User.id == user.id).with_for_update()
        )
        locked_user = user_res.scalars().first()
        if not locked_user:
            raise HTTPException(status_code=404, detail="用户不存在")

        # 3) 置用户为激活/已验证
        locked_user.is_active = True
        locked_user.is_verified = True

        # 4) 消耗激活码
        act.is_used = True
        act.used_by_user_id = locked_user.id
        act.used_at = datetime.utcnow()

    return {"message": "激活成功", "username": user.username}


# =========================================================
# 激活码：管理员分发/管理
# =========================================================

def _gen_code(length: int = 16) -> str:
    # 避免 0/O, 1/I 的歧义字符
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    return "".join(secrets.choice(alphabet) for _ in range(length))


@router.post(
    "/admin/activation-codes/batch",
    summary="批量生成激活码（仅管理员）",
    status_code=status.HTTP_201_CREATED,
)
async def create_activation_codes(
    req: CreateActivationCodesRequest,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    """
    批量生成一次性激活码，写入数据库并返回 code 列表（用于你线下分发）。
    """
    codes: List[str] = []
    rows: List[ActivationCode] = []

    async with db.begin():
        # 简单防重复：生成后查库；更严格可做“插入失败重试”（依赖 unique 约束）
        for _ in range(req.count):
            code = _gen_code(req.length)
            codes.append(code)
            rows.append(ActivationCode(code=code, created_by_user_id=admin.id))

        db.add_all(rows)

    return {"count": len(codes), "codes": codes}


@router.get(
    "/admin/activation-codes",
    summary="查看激活码列表（仅管理员）",
    response_model=List[ActivationCodeRead],
)
async def list_activation_codes(
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
    used: Optional[bool] = None,
    limit: int = 200,
):
    stmt = select(ActivationCode).order_by(ActivationCode.created_at.desc())
    if used is not None:
        stmt = stmt.where(ActivationCode.is_used == used)
    stmt = stmt.limit(min(limit, 500))

    res = await db.execute(stmt)
    return res.scalars().all()


@router.delete(
    "/admin/activation-codes/{code_id}",
    summary="作废激活码（仅管理员）",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_activation_code(
    code_id: int,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    async with db.begin():
        act = await db.get(ActivationCode, code_id)
        if not act:
            raise HTTPException(status_code=404, detail="激活码不存在")
        if act.is_used:
            raise HTTPException(status_code=400, detail="激活码已使用，不能作废")
        await db.delete(act)
    return None
