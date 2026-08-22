# app/api/v1/user.py

from __future__ import annotations

import logging
from decimal import Decimal
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_async_session, managed_transaction
from app.core.users import current_active_user
from app.models.base import User, Position, Transaction, Outcome
from app.schemas.user import HoldingRead, UserSummary, TransactionRead
from app.services.lmsr import quantize_cost
from app.services import site_config as _site_config
from app.services.rank import RANK_THRESHOLDS
from app.services.wealth import compute_users_holdings_value, user_has_halt_holdings

logger = logging.getLogger(__name__)

router = APIRouter()

ZERO = Decimal("0")


# 称号统一走 app.services.rank.RANK_THRESHOLDS（全站一套阈值/文案）


@router.get("/summary", response_model=UserSummary, summary="获取资产概览")
async def get_user_summary(
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    """阶段 3 新契约（spec §6.4）：只返回客户端算不出来的东西。

    每次成交后必被调用 × 每次两遍全仓 LMSR 的时代结束：margin_status
    仅在 debt>0 时算一次 LCV，无债用户零 LMSR 开销。调用时机也随之降频
    （登录 / 手动刷新 / gap reconcile，成交后不再调用）。
    """
    pos_rows = (await db.execute(
        select(Position.outcome_id, Outcome.market_id,
               Position.amount, Position.cost_basis)
        .join(Outcome, Outcome.id == Position.outcome_id)
        .where(Position.user_id == user.id, Position.amount > 0)
    )).all()
    positions = [
        {
            "outcome_id": int(r[0]),
            "market_id": int(r[1]),
            "amount": quantize_cost(r[2]),
            "cost_basis": quantize_cost(r[3]),
        }
        for r in pos_rows
    ]

    hard = await _site_config.get_decimal_or(db, "liquidation_hard_threshold", Decimal("0.2"))
    soft = await _site_config.get_decimal_or(db, "liquidation_soft_threshold", Decimal("0.5"))
    sell_fee_rate = await _site_config.get_decimal_or(db, "sell_fee_rate", ZERO)

    # margin_status 服务端权威（保守 LCV 口径，docs/holdings-value-semantics.md）。
    # 只有 debt>0 才需要跑全仓 LMSR。
    margin_status = "healthy"
    if user.debt > ZERO:
        holdings_lcv = (
            await compute_users_holdings_value(db, user_ids=[user.id])
        ).get(user.id, ZERO)
        margin_ratio = ((user.cash - user.debt + holdings_lcv) / user.debt
                        ).quantize(Decimal("0.000001"))
        if margin_ratio < hard:
            margin_status = "danger"
        elif margin_ratio < soft:
            margin_status = "warning"

    # 流动性危机保护标志：语义不变（review I3）
    liquidation_protected = await user_has_halt_holdings(db, user.id)

    from app.services import title_service as _title_service
    equipped_t = await _title_service.get_equipped_chip(db, user.id)
    my_title_rows = await _title_service.list_my_titles(db, user.id)

    return {
        "cash": quantize_cost(user.cash),   # 6dp——客户端 cash 基线
        "debt": quantize_cost(user.debt),
        "positions": positions,
        "margin_hard_threshold": hard.quantize(Decimal("0.0001")),
        "margin_soft_threshold": soft.quantize(Decimal("0.0001")),
        "sell_fee_rate": sell_fee_rate,
        "rank_thresholds": [
            {"min_net_worth": thr, "title": title} for thr, title in RANK_THRESHOLDS
        ],
        "margin_status": margin_status,
        "liquidation_protected": liquidation_protected,
        "last_liquidated_at": user.last_liquidated_at,
        "equipped_title": (
            {"id": equipped_t.id, "name": equipped_t.name,
             "color": equipped_t.color, "icon": equipped_t.icon}
            if equipped_t else None
        ),
        "all_titles": [
            {"id": t.id, "name": t.name, "color": t.color, "icon": t.icon,
             "description": t.description, "sort_order": t.sort_order}
            for _ut, t in my_title_rows
        ],
    }


@router.get("/holdings", response_model=List[HoldingRead], summary="获取持仓明细")
async def get_my_holdings(
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    """阶段 3 瘦身（spec §6.4）：只返回标签 + 数量/成本；估值下放客户端。"""
    stmt = (
        select(Position)
        .where(Position.user_id == user.id, Position.amount > 0)
        .options(selectinload(Position.outcome).selectinload(Outcome.market))
        .order_by(Position.id.desc())
    )
    positions: List[Position] = (await db.execute(stmt)).scalars().all()
    return [
        HoldingRead(
            outcome_id=pos.outcome_id,
            outcome_label=pos.outcome.label,
            market_id=pos.outcome.market_id,
            market_title=pos.outcome.market.title,
            amount=quantize_cost(pos.amount),
            cost_basis=quantize_cost(pos.cost_basis),
        )
        for pos in positions
    ]


@router.get("/transactions", response_model=List[TransactionRead], summary="获取交易历史")
async def get_my_transactions(
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
    limit: int = Query(50, ge=1, le=200, description="最多返回多少条（按时间倒序）"),
):
    """返回最近 N 条交易记录，附带市场/选项名便于前端展示。"""
    stmt = (
        select(Transaction)
        .where(Transaction.user_id == user.id)
        .options(selectinload(Transaction.outcome).selectinload(Outcome.market))
        .order_by(Transaction.timestamp.desc())
        .limit(limit)
    )
    res = await db.execute(stmt)
    txs: List[Transaction] = res.scalars().all()

    results: List[TransactionRead] = []
    for tx in txs:
        outcome = tx.outcome
        market = outcome.market if outcome else None
        results.append(
            TransactionRead(
                id=tx.id,
                outcome_id=tx.outcome_id,
                market_id=market.id if market else None,
                market_title=market.title if market else None,
                outcome_label=outcome.label if outcome else None,
                type=tx.type,
                shares=tx.shares,
                price=tx.price,
                gross=tx.gross,
                fee=tx.fee,
                cost=tx.cost,
                timestamp=tx.timestamp,
            )
        )
    return results


# ── 用户自助修改昵称 ─────────────────────────
# username 在本系统中只是显示名（登录走 Casdoor SSO 关联 casdoor_id），改动不影响
# 登录。历史成交记录里的 username 是发送时刻的快照，不回填——与 Twitter / Discord
# 等标准做法一致。
import re

_USERNAME_RE = re.compile(r"^[\w一-龥\-]{2,32}$")


class UpdateUsernameRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=32)


@router.patch("/me/username", summary="修改我的昵称")
async def update_my_username(
    req: UpdateUsernameRequest,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    new_name = req.username.strip()
    if not _USERNAME_RE.match(new_name):
        raise HTTPException(
            status_code=400,
            detail="昵称仅支持中文/英文/数字/下划线/连字符，长度 2-32 字符",
        )
    if new_name == user.username:
        return {"username": user.username, "changed": False}

    async with managed_transaction(db):
        # 行锁自己，避免并发改名导致最终落不到唯一索引（虽然 DB 也会兜底）
        locked = (await db.execute(
            select(User).where(User.id == user.id).with_for_update()
        )).scalar_one()

        # unique 校验：另一行用了这个 username？
        dup = (await db.execute(
            select(User.id).where(User.username == new_name).where(User.id != user.id)
        )).first()
        if dup:
            raise HTTPException(status_code=409, detail="该昵称已被占用")

        old_name = locked.username
        locked.username = new_name

    logger.info(
        "USERNAME_UPDATE user_id=%s old=%s new=%s",
        user.id, old_name, new_name,
    )
    return {"username": new_name, "changed": True}
