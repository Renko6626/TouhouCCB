"""管理员强平接口：立即触发一次 sweep。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from app.core.users import current_superuser
from app.models.base import User
from app.services import liquidation_sweep

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/run-now", summary="立即跑一次强平 sweep（仅管理员）")
async def admin_run_liquidation_sweep(
    admin: User = Depends(current_superuser),
):
    """跟 scheduler 同逻辑。不绕过阈值——admin 没有 override 权力。"""
    result = await liquidation_sweep.run_liquidation_sweep_once()
    logger.info(
        "ADMIN_RUN_LIQUIDATION_SWEEP admin_id=%s result=%s",
        admin.id, result,
    )
    return result
