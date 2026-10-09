"""WP6b：PvE 账户资金入口（生成注资 / 注资复活 / 销毁回收）的统一信贷接入。

覆盖：版本自增、无债快路径（不做组合发现/报价）、带债 bot 的销毁回收必须过
扣款后 E 检查、flag OFF 旧行为不变。
"""
import os
import sys
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.database import async_session_maker
from app.models.base import Market, Outcome, Transaction, User
from app.models.bot import BotProfile, BOT_STATUS_ACTIVE, BOT_STATUS_RETIRED
from app.services.credit import flags as credit_flags
from app.services.credit.flags import CreditFlags
from app.services.credit.ownership import WriteOwnership
from app.services.pve import service as pve_svc
from app.services.credit.version import economic_version_of

pytestmark = pytest.mark.asyncio

ZERO = Decimal("0")
SQLITE_URL = "sqlite+aiosqlite:////dev/shm/credit-wp6b.db"
UNIFIED = CreditFlags(
    credit_leverage=Decimal("4"),
    credit_maintenance_ratio=Decimal("0.1"),
)


@pytest.fixture(autouse=True)
def _clean_flags():
    credit_flags.clear_flags()
    yield
    credit_flags.clear_flags()


@pytest_asyncio.fixture
async def writes_enabled(monkeypatch):
    own = WriteOwnership(url=SQLITE_URL)
    await own.acquire()
    monkeypatch.setattr(pve_svc, "OWNERSHIP", own)
    yield own
    await own.release()


async def _state(uid: int) -> tuple[Decimal, Decimal, int]:
    async with async_session_maker() as s:
        u = (await s.execute(select(User).where(User.id == uid))).scalars().one()
        return Decimal(u.cash), Decimal(u.debt), economic_version_of(u)


async def _seed_bot(*, cash: Decimal, debt: Decimal = ZERO, traded: bool = False,
                    status: str = BOT_STATUS_ACTIVE) -> tuple[int, int]:
    """返回 (user_id, profile_id)。traded=True 时插一条 Transaction 逼出退休路径。"""
    async with async_session_maker() as s:
        u = User(
            username=f"bot_{uuid.uuid4().hex[:8]}",
            casdoor_id=uuid.uuid4().hex,
            cash=cash, debt=debt, is_bot=True, is_active=True,
        )
        s.add(u)
        await s.flush()
        p = BotProfile(user_id=u.id, template="hodler", params={}, status=status)
        s.add(p)
        if traded:
            m = Market(title=f"m_{uuid.uuid4().hex[:6]}", liquidity_b=100.0, tags="")
            s.add(m)
            await s.flush()
            o = Outcome(market_id=m.id, label="A", total_shares=Decimal("1"))
            s.add(o)
            await s.flush()
            s.add(Transaction(user_id=u.id, outcome_id=o.id, type="buy",
                              shares=Decimal("1"), cost=Decimal("1")))
        await s.commit()
        await s.refresh(u)
        await s.refresh(p)
        return int(u.id), int(p.id)


# ────────────────────────── 生成 / 注资 ──────────────────────────


async def test_unified_generate_bots_funds_and_bumps_version(writes_enabled):
    credit_flags.set_flags(UNIFIED)
    async with async_session_maker() as s:
        created = await pve_svc.generate_bots(
            s, items=[("hodler", 2)], naming_style="npc",
            initial_cash=Decimal("100"), market_scope=None, operator_user_id=1,
        )
        await s.commit()
    assert len(created) == 2
    for row in created:
        cash, debt, version = await _state(row["user_id"])
        assert cash == Decimal("100.000000") and debt == ZERO
        assert version == 1




async def test_unified_fund_bot_uses_no_debt_fast_path(writes_enabled, monkeypatch):
    """bot 债务为 0 时不得做组合发现/报价（无债快路径）。"""
    uid, pid = await _seed_bot(cash=Decimal("10"))
    credit_flags.set_flags(UNIFIED)

    async def _must_not_discover(*args, **kwargs):
        raise AssertionError("debt-free bot must not discover collateral")

    monkeypatch.setattr(pve_svc, "discover_dependencies", _must_not_discover)
    async with async_session_maker() as s:
        profile = (await s.execute(select(BotProfile).where(BotProfile.id == pid))).scalars().one()
        user = await pve_svc.fund_bot(
            s, profile=profile, amount=Decimal("40"), operator_user_id=1, reason="refill",
        )
        assert user.cash == Decimal("50")
        await s.commit()
    cash, _, version = await _state(uid)
    assert cash == Decimal("50.000000") and version == 1




# ────────────────────────── 销毁回收 ──────────────────────────


async def test_unified_destroy_bot_recovery_bumps_version(writes_enabled):
    uid, pid = await _seed_bot(cash=Decimal("50"), traded=True)
    credit_flags.set_flags(UNIFIED)
    async with async_session_maker() as s:
        profile = (await s.execute(select(BotProfile).where(BotProfile.id == pid))).scalars().one()
        result = await pve_svc.destroy_bot(s, profile=profile, operator_user_id=1)
        await s.commit()
    assert result["mode"] == "retired" and result["recovered_cash"] == 50.0
    cash, debt, version = await _state(uid)
    assert cash == ZERO and debt == ZERO and version == 1
    async with async_session_maker() as s:
        profile = (await s.execute(select(BotProfile).where(BotProfile.id == pid))).scalars().one()
        assert profile.status == BOT_STATUS_RETIRED


async def test_unified_destroy_bot_with_debt_rejected_when_margin_breaks(writes_enabled):
    uid, pid = await _seed_bot(cash=Decimal("5"), debt=Decimal("50"), traded=True)
    credit_flags.set_flags(UNIFIED)
    async with async_session_maker() as s:
        profile = (await s.execute(select(BotProfile).where(BotProfile.id == pid))).scalars().one()
        with pytest.raises(pve_svc.BotOpError) as exc:
            await pve_svc.destroy_bot(s, profile=profile, operator_user_id=1)
        await s.rollback()
    assert exc.value.status_code == 409
    assert "仍有债务" in exc.value.detail
    cash, debt, version = await _state(uid)
    assert cash == Decimal("5.000000") and debt == Decimal("50.000000") and version == 0
    async with async_session_maker() as s:
        profile = (await s.execute(select(BotProfile).where(BotProfile.id == pid))).scalars().one()
        assert profile.status == BOT_STATUS_ACTIVE


async def test_unified_destroy_never_deletes_untraded_debtor(writes_enabled):
    uid, pid = await _seed_bot(cash=Decimal("5"), debt=Decimal("50"))
    credit_flags.set_flags(UNIFIED)
    async with async_session_maker() as s:
        profile = (await s.execute(select(BotProfile).where(BotProfile.id == pid))).scalars().one()
        with pytest.raises(pve_svc.BotOpError, match="仍有债务"):
            await pve_svc.destroy_bot(s, profile=profile, operator_user_id=1)
        await s.rollback()
    assert (await _state(uid))[1] == Decimal("50.000000")




async def test_fund_bot_refreshes_retained_stale_user(writes_enabled):
    uid, pid = await _seed_bot(cash=Decimal("100"))
    credit_flags.set_flags(UNIFIED)
    async with async_session_maker() as s:
        stale = await s.get(User, uid)
        profile = await s.get(BotProfile, pid)
        await s.commit()
        async with async_session_maker() as other:
            current = await other.get(User, uid)
            current.cash = Decimal("40")
            current.economic_version = 1
            await other.commit()
        assert stale.cash == Decimal("100")
        result = await pve_svc.fund_bot(s, profile=profile, amount=Decimal("10"), operator_user_id=1)
        assert result is stale
        await s.commit()
    assert await _state(uid) == (Decimal("50.000000"), ZERO, 2)
