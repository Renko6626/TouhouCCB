"""弹幕系统兑换：HMAC 激活码生成 + 单事务原子兑换。

激活码格式参考朋友的 danmuku 服务（根目录 danmuku.py 同步）：
    base64url(json{yuan, huo, user_id, room_id, ts}) + "." + hmac_sha256_hex

扣款规则：站内 cash → yuan/huo 的汇率固定 1:1，即扣 user.cash = yuan + huo。
两者都允许填 0（但 yuan + huo > 0）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.config import settings
from app.models.base import User
from app.models.redemption import DanmukuExchange


_QUANT = Decimal("0.000001")


def generate_giftcode(
    yuan: float,
    huo: float,
    user_id: str,
    room_id: str,
) -> str:
    """复刻 danmuku.py 的签名逻辑——payload json 严格无空格分隔符，签名为 HMAC-SHA256 hex。"""
    now = int(time.time())
    payload = {
        "yuan": yuan,
        "huo": huo,
        "user_id": user_id,
        "room_id": room_id,
        "ts": now,
    }
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(
        settings.DANMUKU_SECRET_KEY.encode(),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()
    return base64.urlsafe_b64encode(payload_bytes).decode() + "." + signature


class ExchangeError(Exception):
    """兑换失败 code ∈ {INVALID_AMOUNT, INSUFFICIENT_CASH}"""
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


@dataclass
class ExchangeResult:
    id: int
    code_string: str
    yuan: Decimal
    huo: Decimal
    amount: Decimal
    cash_after: Decimal
    timestamp: datetime


async def exchange(
    session: AsyncSession,
    *,
    user_id: int,
    qq_user_id: str,
    room_id: str,
    yuan: Decimal,
    huo: Decimal,
) -> ExchangeResult:
    """单事务原子兑换：行锁 user → 校验余额 → 扣款 → 生成码 → 写记录。

    不在这里 commit，由调用方负责（与 redemption.purchase_code 同模式）。
    """
    if yuan < 0 or huo < 0:
        raise ExchangeError("INVALID_AMOUNT", "金额不能为负")
    amount = (yuan + huo).quantize(_QUANT)
    if amount <= 0:
        raise ExchangeError("INVALID_AMOUNT", "yuan 与 huo 至少一项为正")

    # 行锁 user 防止并发兑换穿透余额
    user_stmt = select(User).where(User.id == user_id).with_for_update()
    user = (await session.execute(user_stmt)).scalar_one()

    if user.cash < amount:
        raise ExchangeError("INSUFFICIENT_CASH", "现金不足")

    user.cash = (user.cash - amount).quantize(_QUANT)
    session.add(user)

    # 生成激活码（HMAC 用浮点输入与 danmuku 侧约定保持一致）
    code_string = generate_giftcode(
        yuan=float(yuan),
        huo=float(huo),
        user_id=qq_user_id,
        room_id=room_id,
    )

    record = DanmukuExchange(
        user_id=user_id,
        qq_user_id=qq_user_id,
        room_id=room_id,
        yuan=yuan.quantize(_QUANT),
        huo=huo.quantize(_QUANT),
        amount=amount,
        code_string=code_string,
        timestamp=datetime.now(timezone.utc),
    )
    session.add(record)
    await session.flush()  # 拿到 record.id

    return ExchangeResult(
        id=record.id,
        code_string=code_string,
        yuan=record.yuan,
        huo=record.huo,
        amount=amount,
        cash_after=user.cash,
        timestamp=record.timestamp,
    )
