"""账户池操作（spec §7）：批量生成 / 注资复活。供 admin_pve API 调用。

资金动作全部走 ledger（record_entry，entry_type=admin_adjust_cash）——
不造新的资金通道；本模块不 commit，事务由调用方（managed_transaction）负责。
"""
from __future__ import annotations

import random
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import Position, Transaction, User
from app.models.bot import (
    BotProfile, BOT_STATUS_ACTIVE, BOT_STATUS_DEAD, BOT_STATUS_RETIRED,
)
from app.models.ledger import LedgerEntry
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
    naming_style: str,                       # "npc" | "lowkey" | "phrase"
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


# ── 改名 / 销毁 ──────────────────────────────────────────────────────────


class BotOpError(Exception):
    """机器人运维操作失败；status_code 直接给 HTTPException 用。"""

    def __init__(self, status_code: int, detail: str, extra: Optional[dict] = None):
        self.status_code, self.detail, self.extra = status_code, detail, extra or {}
        super().__init__(detail)


async def rename_bot(
    db: AsyncSession, *, profile: BotProfile,
    username: Optional[str] = None, style: Optional[str] = None,
) -> tuple[str, str]:
    """给机器人改名。username=直接指定；style=从 naming 词库重抽一个没被占用的。
    返回 (旧名, 新名)。重名抛 BotOpError(409)。"""
    user = (
        await db.execute(select(User).where(User.id == profile.user_id).with_for_update())
    ).scalars().one()
    old = user.username
    if style is not None:
        taken = set((await db.execute(select(User.username))).scalars().all())
        new = generate_usernames(style, 1, taken, random.Random())[0]
    else:
        new = (username or "").strip()
        if not 2 <= len(new) <= 32:
            raise BotOpError(400, "用户名长度需在 2~32 之间")
        if new != old:
            clash = (
                await db.execute(select(User.id).where(User.username == new))
            ).scalars().first()
            if clash is not None:
                raise BotOpError(409, f"用户名 {new!r} 已被占用")
    user.username = new
    return old, new


async def destroy_bot(db: AsyncSession, *, profile: BotProfile, operator_user_id: int) -> dict:
    """销毁机器人，双路径（语义见 docs/pve.md）：

    - **从未交易过** → 真删：Position 空行 / LedgerEntry(初始注资) / BotProfile / User 全清。
      覆盖「生成错了、生成多了」这个主要场景，不留垃圾账号。
    - **交易过** → 清算退休：调用方必须先把持仓平干净（`liquidate_bot_positions`），
      本函数只负责回收现金 + 置 retired。账号与全部流水保留——直接删会留下无主
      LMSR 份额（价格由 Outcome.total_shares 驱动，与 Position 是两套账），
      市场价格里会永远留着一个不存在的用户造成的影响。

    调用方需已持有 managed_transaction。
    """
    user = (
        await db.execute(select(User).where(User.id == profile.user_id).with_for_update())
    ).scalars().one()
    tx_count = (
        await db.execute(
            select(func.count()).select_from(Transaction).where(Transaction.user_id == user.id)
        )
    ).scalar_one()
    held = (
        await db.execute(
            select(func.count()).select_from(Position)
            .where(Position.user_id == user.id, Position.amount > 0)
        )
    ).scalar_one()

    name = user.username
    if tx_count == 0 and held == 0:
        # 从未参与过市场 → 真删。按外键顺序清，初始注资那条 ledger 一并删除
        # （该账号没有任何市场行为，这条流水没有审计价值，留着反而挡住 FK）
        for model in (Position, LedgerEntry):
            for row in (
                await db.execute(select(model).where(model.user_id == user.id))
            ).scalars().all():
                await db.delete(row)
        await db.delete(profile)
        await db.delete(user)
        return {"mode": "deleted", "username": name, "recovered_cash": 0.0, "sold": []}

    if held > 0:
        raise BotOpError(409, "仍有持仓未平，无法退休——请重试销毁（平仓是幂等的）",
                         {"open_positions": held})

    recovered = user.cash
    if recovered > 0:
        user.cash = Decimal("0")
        await record_entry(
            db,
            user=user,
            entry_type="admin_adjust_cash",
            cash_delta=-recovered,
            debt_delta=Decimal("0"),
            daily_rate=None,
            operator_user_id=operator_user_id,
            reason="PvE 机器人销毁：清算回收现金",
        )
    profile.status = BOT_STATUS_RETIRED
    user.is_active = False
    return {"mode": "retired", "username": name,
            "recovered_cash": float(recovered), "sold": []}
