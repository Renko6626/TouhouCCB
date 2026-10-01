"""兑换码模块服务层：CSV 解析、购买事务、库存查询。

统一信贷（WP6b）：`purchase_code` 是现金消费入口（flag=unified_credit_enabled 时）
- 单写实例守卫 `OWNERSHIP.require_writes()`；
- 锁外做依赖发现（无债用户走单行快路径），按品种门闩（抵押品共享）后锁 user；
- 沿用「有债禁止兑换」，再跑 `check_cash_spend`（版本复检 + 交易后 E / 冻结判定）；
- 通过后扣款并自增 `economic_version`。
开关关闭时逐字段保持旧行为（不查依赖、不置版本、不多任何一行查询）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import managed_transaction
from sqlalchemy.future import select
from sqlalchemy import update
from fastapi import HTTPException

from app.services.credit.cash import available_cash, has_foreign_debt
from app.models.base import User
from app.models.redemption import (
    RedemptionPartner, RedemptionBatch, RedemptionCode, RedemptionTransaction,
    BatchStatus, CodeStatus,
)
from app.services.credit import flags as credit_flags
from app.services.credit.gates import GATES
from app.services.credit.ownership import OWNERSHIP
from app.services.credit.risk import (
    REASON_CREDIT_FROZEN,
    REASON_FROZEN_BY_OPERATOR,
    REASON_INSUFFICIENT_INITIAL_MARGIN,
    REASON_VERSION_CONFLICT,
    DependencySet,
    check_cash_spend,
    discover_dependencies,
)
from app.services.credit.thresholds import RiskThresholds
from app.services.credit.version import bump_economic_version


_MAX_CODE_LEN = 128
_QUANT = Decimal("0.000001")
ZERO = Decimal("0")
ONE = Decimal("1")

_RISK_DENY_DETAILS = {
    REASON_CREDIT_FROZEN: "账号已因坏账冻结，仅允许充值 / 还款 / 核销",
    REASON_FROZEN_BY_OPERATOR: "运营已开启停增险，暂缓消费类操作",
    REASON_INSUFFICIENT_INITIAL_MARGIN: "扣款后不满足初始保证金门槛",
    REASON_VERSION_CONFLICT: "经济版本冲突（并发写入），请重试",
}


def _unified_thresholds() -> Optional[RiskThresholds]:
    """flag 关闭 → None（走 legacy）；flag 开启但门槛缺失 → fail-closed。"""
    if OWNERSHIP.reason is not None:
        OWNERSHIP.require_writes()
    flags = credit_flags.get_flags()
    if not flags.unified_credit_enabled:
        return None
    thresholds = flags.thresholds
    if thresholds is None:
        raise HTTPException(status_code=503, detail="统一信贷风险引擎不可用，已拒绝消费类操作")
    return thresholds


async def _deps_for_spend(session: AsyncSession, user_id: int) -> Optional[DependencySet]:
    """锁外依赖发现：无债返回最小依赖集（不读持仓/快照），有债返回完整组合。"""
    row = (await session.execute(
        select(User.cash, User.debt, User.debt_last_accrued_at, User.economic_version)
        .where(User.id == user_id)
    )).first()
    if row is None:
        return None
    cash, debt, last_accrued, version = row
    if Decimal(debt) > ZERO:
        return await discover_dependencies(session, user_id)
    return DependencySet(
        economic_version=int(version or 0),
        cash=Decimal(cash),
        debt=Decimal(debt),
        debt_last_accrued_at=last_accrued,
        groups=(),
        holdings={},
    )


def _risk_deny_detail(reason: Optional[str]) -> str:
    return _RISK_DENY_DETAILS.get(reason, f"风险检查未通过：{reason}")
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
    """购买失败 code ∈ {INSUFFICIENT_CASH, OUTSTANDING_DEBT, SOLD_OUT, BATCH_NOT_ACTIVE, BATCH_NOT_FOUND, PER_USER_LIMIT_REACHED}"""
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

    统一信贷 ON：先锁外发现依赖 + 门闩，再进 `_purchase_code_impl` 锁行；
    版本复检与冻结 / 保证金判定在 `check_cash_spend` 内完成。
    """
    thresholds = _unified_thresholds()
    if thresholds is None:
        return await _purchase_code_impl(
            session, user_id=user_id, batch_id=batch_id, deps=None, thresholds=None,
        )
    OWNERSHIP.require_writes()
    if session.new or session.dirty or session.deleted:
        raise RuntimeError("purchase_code requires a clean session before risk admission")
    deps = await _deps_for_spend(session, user_id)
    await session.commit()
    async with GATES.hold(shared=() if deps is None else deps.groups):
        async with managed_transaction(session):
            return await _purchase_code_impl(
                session, user_id=user_id, batch_id=batch_id, deps=deps, thresholds=thresholds,
            )


async def _purchase_code_impl(
    session: AsyncSession,
    *,
    user_id: int,
    batch_id: int,
    deps: Optional[DependencySet],
    thresholds: Optional[RiskThresholds],
) -> PurchaseResult:
    user_stmt = select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    user = (await session.execute(user_stmt)).scalar_one()

    batch = await session.get(RedemptionBatch, batch_id)
    if batch is None:
        raise PurchaseError("BATCH_NOT_FOUND")
    if batch.status != BatchStatus.ACTIVE:
        raise PurchaseError("BATCH_NOT_ACTIVE")

    # 与借款/还款共享 user 行锁，按最新债务判断；现金再多也不能带债兑换。
    if user.debt > 0 or await has_foreign_debt(session, user.id):
        raise PurchaseError("OUTSTANDING_DEBT")

    if await available_cash(session, user) < batch.unit_price:
        raise PurchaseError("INSUFFICIENT_CASH")

    if thresholds is not None:
        # 锁成功 ⇒ 用户存在 ⇒ 锁外预读一定拿到了依赖集
        assert deps is not None
        decision = await check_cash_spend(
            session, user=user, deps=deps, spend=batch.unit_price,
            thresholds=thresholds, partial_pct=ONE, now=datetime.now(timezone.utc),
        )
        if not decision.allowed:
            raise HTTPException(
                status_code=409,
                detail=f"统一信贷准入拒绝：{_risk_deny_detail(decision.reason)}",
            )

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

    OWNERSHIP.require_writes()
    now = datetime.now(timezone.utc)
    user.cash = (user.cash - batch.unit_price).quantize(_QUANT)
    code.status = CodeStatus.SOLD
    code.bought_by_user_id = user_id
    code.bought_at = now

    if thresholds is not None:
        bump_economic_version(user)

    session.add(user)
    session.add(code)

    partner = await session.get(RedemptionPartner, batch.partner_id)

    # 写资金流水审计行（同事务）
    from app.services import audit_service
    audit_service.record(
        session, "redeem_purchase",
        user_id=user_id,
        payload={"batch_id": batch.id, "code_id": code.id, "amount": batch.unit_price},
        user_after=audit_service.user_snapshot(user),
    )
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


async def count_redeemed_for_batch(session: AsyncSession, batch_id: int) -> int:
    stmt = select(func.count()).select_from(RedemptionCode).where(
        RedemptionCode.batch_id == batch_id,
        RedemptionCode.redeemed_at.is_not(None),
    )
    return int((await session.execute(stmt)).scalar_one())


async def fulfill_code(session: AsyncSession, code_id: int, admin_id: int, note: str) -> None:
    """原子核销；状态和审计同事务，调用者 commit。条件更新也兼容 SQLite。"""
    now = datetime.now(timezone.utc)
    result = await session.execute(
        update(RedemptionCode).where(
            RedemptionCode.id == code_id,
            RedemptionCode.status == CodeStatus.SOLD,
            RedemptionCode.bought_by_user_id.is_not(None),
            RedemptionCode.redeemed_at.is_(None),
        ).values(
            redeemed_at=now, redeemed_by_admin_id=admin_id, redemption_note=note.strip(),
        ).execution_options(synchronize_session=False)
    )
    code = (await session.execute(
        select(RedemptionCode).where(RedemptionCode.id == code_id)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if code is None:
        raise HTTPException(status_code=404, detail="兑换码不存在")
    if result.rowcount != 1:
        if code.redeemed_at is not None:
            # 展示时间统一使用明确的 UTC，前端列表再转工作人员本地时间。
            when = code.redeemed_at.replace(tzinfo=timezone.utc).isoformat()
            raise HTTPException(status_code=409, detail=f"该兑换码已于 {when} 核销，请勿重复发放")
        raise HTTPException(status_code=409, detail="兑换码尚未售出，不能核销")
    from app.services import audit_service
    audit_service.record(
        session, "redeem_fulfill", user_id=code.bought_by_user_id,
        operator_user_id=admin_id, ref_table="redemption_code", ref_id=code.id,
        payload={"batch_id": code.batch_id, "redeemed_at": now, "note": note.strip()},
        ts=now,
    )


async def revoke_fulfillment(
    session: AsyncSession, code_id: int, admin_id: int, reason: str, expected_redeemed_at: datetime,
) -> None:
    """撤销误核销，并保留原记录及原因；用户个人标记、销售状态和资金不变。"""
    reason = reason.strip()
    if not reason:
        raise HTTPException(status_code=400, detail="请填写撤销原因")
    code = (await session.execute(
        select(RedemptionCode).where(RedemptionCode.id == code_id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if code is None:
        raise HTTPException(status_code=404, detail="兑换码不存在")
    if code.redeemed_at is None:
        raise HTTPException(status_code=409, detail="该兑换码尚未核销")
    current_time = code.redeemed_at.replace(tzinfo=timezone.utc) if code.redeemed_at.tzinfo is None else code.redeemed_at.astimezone(timezone.utc)
    expected_time = expected_redeemed_at.replace(tzinfo=timezone.utc) if expected_redeemed_at.tzinfo is None else expected_redeemed_at.astimezone(timezone.utc)
    if current_time != expected_time:
        raise HTTPException(status_code=409, detail="核销记录已变化，请刷新后重试")
    previous = {
        "batch_id": code.batch_id, "reason": reason,
        "previous_redeemed_at": code.redeemed_at,
        "previous_redeemed_by_admin_id": code.redeemed_by_admin_id,
        "previous_note": code.redemption_note,
    }
    result = await session.execute(
        update(RedemptionCode).where(
            RedemptionCode.id == code_id, RedemptionCode.redeemed_at == code.redeemed_at,
        ).values(
            redeemed_at=None, redeemed_by_admin_id=None, redemption_note="",
        ).execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise HTTPException(status_code=409, detail="核销状态已变化，请刷新后重试")
    from app.services import audit_service
    audit_service.record(
        session, "redeem_fulfill_revoke", user_id=code.bought_by_user_id,
        operator_user_id=admin_id, ref_table="redemption_code", ref_id=code.id,
        payload=previous,
    )


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
