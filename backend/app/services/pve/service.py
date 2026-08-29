"""账户池操作（spec §7）：批量生成 / 注资复活。供 admin_pve API 调用。

资金动作全部走 ledger（record_entry，entry_type=admin_adjust_cash）——
不造新的资金通道；本模块不 commit，事务由调用方（managed_transaction）负责。
"""
from __future__ import annotations

import random
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import User
from app.models.bot import BotProfile, BOT_STATUS_ACTIVE, BOT_STATUS_DEAD
from app.services.ledger_service import record_entry
from app.services.pve.attention import ATTENTION_DEFAULTS
from app.services.pve.naming import generate_usernames
from app.services.pve.templates import TEMPLATE_REGISTRY

_RETAIL_PRESETS = ["worker", "evening", "owl", "loose"]

# 生成时不做数值扰动的键（语义非"强度"）
_NO_PERTURB = {"outcome_id", "price_low", "price_high", "active_preset"}


def spawn_params(template: str, rng: random.Random) -> dict:
    """模板默认值 + 注意力默认值 → 随机扰动出个体人格，全量落库（管理页可直接看/改）。"""
    base = dict(ATTENTION_DEFAULTS)
    base.update(TEMPLATE_REGISTRY[template].default_params)
    out = {}
    for k, v in base.items():
        if k not in _NO_PERTURB and isinstance(v, (int, float)) and not isinstance(v, bool):
            factor = rng.uniform(0.75, 1.3)
            out[k] = round(v * factor) if isinstance(v, int) else round(v * factor, 4)
        else:
            out[k] = v
    # 模板 default_params 里 active_preset="always" 视为量化型（全天候，不抽作息）；
    # 其余按散户抽典型作息——新模板凭自己的默认值声明类型，这里不维护清单
    if out.get("active_preset") != "always":
        out["active_preset"] = rng.choice(_RETAIL_PRESETS)
    out["hour_offset"] = round(rng.uniform(-1.5, 1.5), 2)
    out["alert_threshold"] = round(rng.uniform(0.04, 0.15), 3)
    out["alert_prob"] = round(rng.uniform(0.25, 0.85), 2)
    out["alert_cooldown_sec"] = rng.randint(900, 3600)
    return out


async def generate_bots(
    db: AsyncSession,
    *,
    items: List[tuple[str, int]],           # [(template, count), ...]
    naming_style: str,                       # "npc" | "lowkey"
    initial_cash: Decimal,
    market_scope: Optional[List[int]],
    operator_user_id: int,
) -> List[dict]:
    for template, _ in items:
        if template not in TEMPLATE_REGISTRY:
            raise ValueError(f"未知模板：{template}")
    total = sum(c for _, c in items)
    taken = set(
        (await db.execute(select(User.username))).scalars().all()
    )
    rng = random.Random()
    names = generate_usernames(naming_style, total, taken, rng)

    created: List[dict] = []
    i = 0
    for template, count in items:
        for _ in range(count):
            user = User(username=names[i], is_bot=True, is_active=True, cash=Decimal("0"))
            db.add(user)
            await db.flush()  # 拿 user.id
            if initial_cash > 0:
                user.cash = initial_cash
                await record_entry(
                    db,
                    user=user,
                    entry_type="admin_adjust_cash",
                    cash_delta=initial_cash,
                    debt_delta=Decimal("0"),
                    daily_rate=None,
                    operator_user_id=operator_user_id,
                    reason="PvE 机器人初始注资",
                )
            profile = BotProfile(
                user_id=user.id,
                template=template,
                params=spawn_params(template, rng),
                market_scope=market_scope,
                status=BOT_STATUS_ACTIVE,
            )
            db.add(profile)
            await db.flush()
            created.append(
                {"profile_id": profile.id, "user_id": user.id, "username": user.username, "template": template}
            )
            i += 1
    return created


async def fund_bot(
    db: AsyncSession,
    *,
    profile: BotProfile,
    amount: Decimal,
    operator_user_id: int,
    reason: Optional[str] = None,
) -> User:
    """注资；dead 机器人顺带复活（paused 保持 paused，管理员显式恢复）。"""
    user = (
        await db.execute(select(User).where(User.id == profile.user_id).with_for_update())
    ).scalars().one()
    user.cash += amount
    await record_entry(
        db,
        user=user,
        entry_type="admin_adjust_cash",
        cash_delta=amount,
        debt_delta=Decimal("0"),
        daily_rate=None,
        operator_user_id=operator_user_id,
        reason=reason or "PvE 机器人注资/复活",
    )
    if profile.status == BOT_STATUS_DEAD:
        profile.status = BOT_STATUS_ACTIVE
    return user
