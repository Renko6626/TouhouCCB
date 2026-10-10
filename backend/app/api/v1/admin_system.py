"""管理员系统维护：只操作当前受 Docker 管理的后端进程。"""
from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.users import current_superuser
from app.models.base import User
from app.services import backend_restart

router = APIRouter()


class RestartRequest(BaseModel):
    instance_id: str = Field(min_length=1, max_length=64)


@router.get("/status")
async def system_status(admin: User = Depends(current_superuser),
                        db: AsyncSession = Depends(get_async_session)):
    return await backend_restart.status(db)


@router.post("/restart", status_code=202)
async def restart_backend(req: RestartRequest, background_tasks: BackgroundTasks,
                          admin: User = Depends(current_superuser),
                          db: AsyncSession = Depends(get_async_session)):
    result = await backend_restart.request_restart(db, admin.id, req.instance_id)
    background_tasks.add_task(backend_restart.stop_process)
    return result
