"""管理员统计接口：平台资产分布、不平等指标等。"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.users import current_superuser
from app.models.base import User
from app.services import site_config
from app.services.wealth import compute_users_holdings_value
from app.services.wealth_stats import compute_wealth_distribution

logger = logging.getLogger(__name__)
router = APIRouter()

ZERO = Decimal("0")


@router.get("/wealth", summary="平台资产分布统计（仅管理员）")
async def wealth_stats(
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    """汇总所有 is_active=True 用户的 net_worth（cash + 持仓清算价 - debt），
    返回均值/中位数/方差/分位数/基尼系数/按称号阈值分桶统计。

    口径与 /user/summary、/market/leaderboard 一致——持仓清算价 = LMSR 全部
    卖出 gross × (1-fee)，统一走 services.wealth.compute_users_holdings_value。
    """
    # PvE：wealth_stats_include_bots=false 时宏观统计只看真人
    include_bots = await site_config.get_bool_or(db, "wealth_stats_include_bots", True)
    users_stmt = select(User).where(User.is_active == True)  # noqa: E712
    if not include_bots:
        users_stmt = users_stmt.where(User.is_bot == False)  # noqa: E712
    users = (await db.execute(users_stmt)).scalars().all()
    if not users:
        return compute_wealth_distribution(
            [], total_cash=0.0, total_debt=0.0, total_holdings_value=0.0,
        )

    holdings_by_user = await compute_users_holdings_value(
        db,
        user_ids=[u.id for u in users],
    )

    net_worths: List[float] = []
    total_cash = ZERO
    total_debt = ZERO
    total_holdings = ZERO
    for u in users:
        holdings = holdings_by_user.get(u.id, ZERO)
        net_worth = u.cash + holdings - u.debt
        net_worths.append(float(net_worth))
        total_cash += u.cash
        total_debt += u.debt
        total_holdings += holdings

    logger.info(
        "WEALTH_STATS admin_id=%s n=%s mean=%s",
        admin.id, len(net_worths), sum(net_worths) / len(net_worths) if net_worths else 0,
    )

    return compute_wealth_distribution(
        net_worths,
        total_cash=float(total_cash),
        total_debt=float(total_debt),
        total_holdings_value=float(total_holdings),
    )
