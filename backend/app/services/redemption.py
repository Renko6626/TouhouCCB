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
    RedemptionPartner, RedemptionBatch, RedemptionCode, RedemptionTransaction,
    BatchStatus, CodeStatus,
)


_MAX_CODE_LEN = 128
_QUANT = Decimal("0.000001")
# 单用户在单个批次的累计购买上限，防 1 个用户秒杀整批
# 不走 site_config 是有意为之：YAGNI，若实际产生分歧再做成可配置
_PER_USER_PER_BATCH_LIMIT = 5


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
    """购买失败 code ∈ {INSUFFICIENT_CASH, SOLD_OUT, BATCH_NOT_ACTIVE, BATCH_NOT_FOUND, PER_USER_LIMIT_REACHED}"""
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

    # 单用户单批次累计上限校验
    from sqlalchemy import func as _func
    owned_stmt = select(_func.count()).select_from(RedemptionCode).where(
        RedemptionCode.batch_id == batch_id,
        RedemptionCode.bought_by_user_id == user_id,
    )
    owned = int((await session.execute(owned_stmt)).scalar_one())
    if owned >= _PER_USER_PER_BATCH_LIMIT:
        raise PurchaseError("PER_USER_LIMIT_REACHED")

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

    now = datetime.now(timezone.utc)
    user.cash = (user.cash - batch.unit_price).quantize(_QUANT)
    code.status = CodeStatus.SOLD
    code.bought_by_user_id = user_id
    code.bought_at = now

    session.add(user)
    session.add(code)

    partner = await session.get(RedemptionPartner, batch.partner_id)

    # 写资金流水审计行（同事务）
    session.add(RedemptionTransaction(
        user_id=user_id,
        code_id=code.id,
        batch_id=batch.id,
        partner_id=partner.id if partner else None,
        batch_name_snapshot=batch.name,
        partner_name_snapshot=partner.name if partner else "",
        amount=batch.unit_price,
        timestamp=now,
    ))

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


# ========== 库存查询 / 列表 / CSV 导入 ==========
from sqlalchemy import func


async def count_available_for_batch(session: AsyncSession, batch_id: int) -> int:
    stmt = select(func.count()).select_from(RedemptionCode).where(
        RedemptionCode.batch_id == batch_id,
        RedemptionCode.status == CodeStatus.AVAILABLE,
    )
    return int((await session.execute(stmt)).scalar_one())


async def count_total_for_batch(session: AsyncSession, batch_id: int) -> int:
    stmt = select(func.count()).select_from(RedemptionCode).where(
        RedemptionCode.batch_id == batch_id,
    )
    return int((await session.execute(stmt)).scalar_one())


async def list_active_batches_with_stock(session: AsyncSession):
    """返回 [(batch, partner, available_count)]，仅 active 批次 + active 合作方 + 库存 > 0。"""
    batches_stmt = select(RedemptionBatch).where(
        RedemptionBatch.status == BatchStatus.ACTIVE,
    )
    batches = list((await session.execute(batches_stmt)).scalars().all())
    result = []
    for b in batches:
        partner = await session.get(RedemptionPartner, b.partner_id)
        if partner is None or not partner.is_active:
            continue
        avail = await count_available_for_batch(session, b.id)
        if avail <= 0:
            continue
        result.append((b, partner, avail))
    return result


async def import_codes_dry_run(
    session: AsyncSession, batch_id: int, csv_text: str,
):
    """预检：解析 + 与已有码比对。返回 (new, duplicate, invalid)。"""
    valid, invalid = parse_csv_codes(csv_text)
    if not valid:
        return [], [], invalid
    stmt = select(RedemptionCode.code_string).where(
        RedemptionCode.code_string.in_(valid),
    )
    existing = set((await session.execute(stmt)).scalars().all())
    new = [c for c in valid if c not in existing]
    dup = [c for c in valid if c in existing]
    return new, dup, invalid


async def import_codes_commit(
    session: AsyncSession, batch_id: int, csv_text: str,
) -> dict:
    """真正写入。重复/非法跳过。调用方负责 commit。"""
    new, dup, invalid = await import_codes_dry_run(session, batch_id, csv_text)
    for cs in new:
        session.add(RedemptionCode(batch_id=batch_id, code_string=cs))
    return {
        "inserted": len(new),
        "skipped_duplicate": len(dup),
        "skipped_invalid": len(invalid),
    }
