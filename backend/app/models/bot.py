"""PvE 机器人账户池模型。

一机器人一行，user_id 唯一关联 User（该 User 行 is_bot=true、casdoor_id=NULL）。
行为模板代码在 app/services/pve/templates.py；本表只存模板名 + 个体参数。
设计 spec：docs/superpowers/specs/2026-08-29-pve-bots-design.md
"""
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import SQLModel, Field
from sqlalchemy import Column, DateTime, JSON

# status 取值（应用层收敛，不加 DB enum，沿用 Transaction.type 惯例）
BOT_STATUS_ACTIVE = "active"    # 参与调度
BOT_STATUS_PAUSED = "paused"    # 管理员暂停
BOT_STATUS_DEAD = "dead"        # 总资产（cash+LCV）低于水位自动置；ledger 注资后管理员拨回 active
BOT_STATUSES = frozenset({BOT_STATUS_ACTIVE, BOT_STATUS_PAUSED, BOT_STATUS_DEAD})


class BotProfile(SQLModel, table=True):
    __tablename__ = "bot_profile"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", unique=True, index=True)

    # 人格模板名，见 services/pve/templates.py 的 TEMPLATE_REGISTRY
    template: str = Field(max_length=32)

    # 覆盖模板默认值的个体参数（生成时随机扰动后落库）
    params: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))

    status: str = Field(default=BOT_STATUS_ACTIVE, max_length=16, index=True)

    # null=全市场；[market_id, ...]=只在指定市场活动（暖场场景）
    market_scope: Optional[list] = Field(default=None, sa_column=Column(JSON, nullable=True))

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_type=DateTime(timezone=True),
    )
    last_trade_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))
