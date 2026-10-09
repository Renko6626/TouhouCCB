"""弹幕系统兑换：HMAC 激活码生成 + 单事务原子兑换。

激活码格式参考朋友的 danmuku 服务（根目录 danmuku.py 同步）：
    base64url(json{yuan, huo, user_id, room_id, ts}) + "." + hmac_sha256_hex

扣款规则：站内 cash → yuan/huo 的汇率固定 1:1，即扣 user.cash = yuan + huo。
两者都允许填 0（但 yuan + huo > 0）。

统一信贷（WP6b）：账户级准入 时，扣款入口先 `OWNERSHIP.require_writes()`，
锁外发现依赖（无债走单行快路径）+ 按品种门闩，再锁 user、跑 `check_cash_spend`
（版本复检 + 交易后 E / 冻结判定），通过后扣款并自增 `economic_version`。
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
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import managed_transaction
from sqlalchemy.future import select

from app.core.config import settings
from app.services.credit.cash import available_cash, has_foreign_debt
from app.models.base import User
from app.models.redemption import DanmukuExchange
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
    if OWNERSHIP.reason is not None:
        OWNERSHIP.require_writes()
    flags = credit_flags.get_flags()
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
    """兑换失败 code ∈ {INVALID_AMOUNT, INSUFFICIENT_CASH, OUTSTANDING_DEBT}"""
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

    thresholds = _unified_thresholds()
    OWNERSHIP.require_writes()
    if session.new or session.dirty or session.deleted:
        raise RuntimeError("exchange requires a clean session before risk admission")
    deps = await _deps_for_spend(session, user_id)
    await session.commit()
    async with GATES.hold(shared=() if deps is None else deps.groups):
        async with managed_transaction(session):
            return await _exchange_impl(
                session, user_id=user_id, qq_user_id=qq_user_id, room_id=room_id,
                yuan=yuan, huo=huo, amount=amount, deps=deps, thresholds=thresholds,
            )


async def _exchange_impl(
    session: AsyncSession,
    *,
    user_id: int,
    qq_user_id: str,
    room_id: str,
    yuan: Decimal,
    huo: Decimal,
    amount: Decimal,
    deps: Optional[DependencySet],
    thresholds: Optional[RiskThresholds],
) -> ExchangeResult:
    # 行锁 user 防止并发兑换穿透余额
    user_stmt = select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    user = (await session.execute(user_stmt)).scalar_one()

    if user.debt > 0 or await has_foreign_debt(session, user.id):
        raise ExchangeError("OUTSTANDING_DEBT", "请先还清全部借款（含利息），再购买或兑换激活码")

    if await available_cash(session, user) < amount:
        raise ExchangeError("INSUFFICIENT_CASH", "现金不足")

    assert deps is not None
    decision = await check_cash_spend(
        session, user=user, deps=deps, spend=amount,
        thresholds=thresholds, partial_pct=ONE, now=datetime.now(timezone.utc),
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=409,
            detail=f"统一信贷准入拒绝：{_risk_deny_detail(decision.reason)}",
        )

    OWNERSHIP.require_writes()
    user.cash = (user.cash - amount).quantize(_QUANT)
    bump_economic_version(user)
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
    from app.services import audit_service
    audit_service.record(
        session, "danmuku_exchange",
        user_id=user_id,
        ref_table="danmuku_exchange", ref_id=record.id,
        payload={"yuan": record.yuan, "huo": record.huo, "amount": amount,
                 "qq_user_id": qq_user_id, "room_id": room_id},
        user_after=audit_service.user_snapshot(user),
    )

    return ExchangeResult(
        id=record.id,
        code_string=code_string,
        yuan=record.yuan,
        huo=record.huo,
        amount=amount,
        cash_after=user.cash,
        timestamp=record.timestamp,
    )
