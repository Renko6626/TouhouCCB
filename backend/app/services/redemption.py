"""兑换码模块服务层：CSV 解析、购买事务、库存查询。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Tuple

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.models.base import User
from app.models.redemption import (
    RedemptionPartner, RedemptionBatch, RedemptionCode,
    BatchStatus, CodeStatus,
)


_MAX_CODE_LEN = 128
_QUANT = Decimal("0.000001")


def parse_csv_codes(text: str) -> Tuple[List[str], List[str]]:
    """解析 CSV 文本，返回 (valid_codes, invalid_codes)。

    规则：
    - 按行切分，每行 strip
    - 跳过空行
    - 第一行如果是 "code"（小写）视为表头，跳过
    - 长度超过 _MAX_CODE_LEN 的归入 invalid
    - 合法码内部去重（保持首次出现顺序）
    """
    lines = [ln.strip() for ln in text.splitlines()]
    while lines and lines[0] == "":
        lines.pop(0)
    if lines and lines[0].lower() == "code":
        lines = lines[1:]

    valid: List[str] = []
    invalid: List[str] = []
    seen = set()
    for ln in lines:
        if not ln:
            continue
        if len(ln) > _MAX_CODE_LEN:
            invalid.append(ln)
            continue
        if ln in seen:
            continue
        seen.add(ln)
        valid.append(ln)
    return valid, invalid


class PurchaseError(Exception):
    """购买失败，code ∈ {INSUFFICIENT_CASH, SOLD_OUT, BATCH_NOT_ACTIVE, BATCH_NOT_FOUND}"""
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


@dataclass
class PurchaseResult:
    code_id: int
    code_string: str
    batch_id: int
    batch_name: str
    partner_name: str
    partner_website_url: str
    description: str
    paid_amount: Decimal
    cash_after: Decimal


async def purchase_code(
    session: AsyncSession,
    *,
    user_id: int,
    batch_id: int,
) -> PurchaseResult:
    """单事务原子购买：行锁 user → 校验 → SKIP LOCKED 抢一个码 → 扣款 → 标记码。
    不在这里 commit，由调用方负责。
    """
    user_stmt = select(User).where(User.id == user_id).with_for_update()
    user = (await session.execute(user_stmt)).scalar_one()

    batch = await session.get(RedemptionBatch, batch_id)
    if batch is None:
        raise PurchaseError("BATCH_NOT_FOUND")
    if batch.status != BatchStatus.ACTIVE:
        raise PurchaseError("BATCH_NOT_ACTIVE")

    if user.cash < batch.unit_price:
        raise PurchaseError("INSUFFICIENT_CASH")

    code_stmt = (
        select(RedemptionCode)
        .where(
            RedemptionCode.batch_id == batch_id,
            RedemptionCode.status == CodeStatus.AVAILABLE,
        )
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    code = (await session.execute(code_stmt)).scalar_one_or_none()
    if code is None:
        raise PurchaseError("SOLD_OUT")

    user.cash = (user.cash - batch.unit_price).quantize(_QUANT)
    code.status = CodeStatus.SOLD
    code.bought_by_user_id = user_id
    code.bought_at = datetime.now(timezone.utc)

    session.add(user)
    session.add(code)

    partner = await session.get(RedemptionPartner, batch.partner_id)

    return PurchaseResult(
        code_id=code.id,
        code_string=code.code_string,
        batch_id=batch.id,
        batch_name=batch.name,
        partner_name=partner.name if partner else "",
        partner_website_url=partner.website_url if partner else "",
        description=batch.description,
        paid_amount=batch.unit_price,
        cash_after=user.cash,
    )
