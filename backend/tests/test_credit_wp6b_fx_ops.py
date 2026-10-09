"""Liquidity funding and withdrawals retain unified-credit gates and pool versions."""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.database import async_session_maker
from app.models.audit import AuditEvent
from app.models.base import SiteConfig
from app.models.fx import FxPair, FxTrade, FxTreasury
from app.services import site_config
from app.services.credit import flags as credit_flags
from app.services.credit.flags import CreditFlags
from app.services.credit.gates import GATES
from app.services.credit.keys import GroupKey
from app.services.credit.ownership import WriteOwnership
from app.services.fx import liquidity

pytestmark = pytest.mark.asyncio

ZERO = Decimal("0")
SQLITE_URL = "sqlite+aiosqlite:////dev/shm/credit-wp6b.db"
UNIFIED = CreditFlags(
    unified_credit_enabled=True,
    credit_leverage=Decimal("4"),
    credit_maintenance_ratio=Decimal("0.1"),
)
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)




class _RecordingGates:
    """记录 hold 参数后委托真门闩（验证调用方拿到独占键）。"""

    def __init__(self, inner):
        self.inner = inner
        self.calls: list[tuple[tuple, tuple]] = []

    def hold(self, *, exclusive=(), shared=()):
        self.calls.append((tuple(exclusive), tuple(shared)))
        return self.inner.hold(exclusive=exclusive, shared=shared)


@pytest.fixture(autouse=True)
def _clean_flags():
    credit_flags.clear_flags()
    site_config.clear_cache()
    yield
    credit_flags.clear_flags()
    site_config.clear_cache()


@pytest_asyncio.fixture
async def writes_enabled(monkeypatch):
    own = WriteOwnership(url=SQLITE_URL)
    await own.acquire()
    monkeypatch.setattr(liquidity, "OWNERSHIP", own)
    yield own
    await own.release()


@pytest.fixture(autouse=True)
def _no_publish(monkeypatch):
    async def _noop(_trade):
        return None
    monkeypatch.setattr("app.services.fx.market_data.publish_trade", _noop)


async def _seed_pair(*, code: str = "TST", status: str = "trading", reduce_only: bool = False,
                     gold: str = "100", foreign: str = "100", initial: str = "1",
                     fx_enabled: bool = True) -> int:
    async with async_session_maker() as s:
        if fx_enabled:
            s.add(SiteConfig(key="fx_enabled", value="true", value_type="bool"))
        p = FxPair(
            currency_code=f"{code}{uuid.uuid4().hex[:4]}", currency_name=code,
            status=status, reduce_only=reduce_only,
            gold_reserve=Decimal(gold), foreign_reserve=Decimal(foreign),
            initial_price=Decimal(initial),
        )
        s.add(p)
        await s.flush()
        s.add(FxTreasury(pair_id=p.id, gold_balance=Decimal(gold), foreign_balance=Decimal(foreign)))
        await s.commit()
        return int(p.id)


async def _pair(pid: int) -> FxPair:
    async with async_session_maker() as s:
        return (await s.execute(select(FxPair).where(FxPair.id == pid))).scalars().one()




# ────────────────────────── fund / withdraw ──────────────────────────


async def test_unified_fund_and_withdraw_bump_pool_version(writes_enabled, monkeypatch):
    pid = await _seed_pair(code="FUND")
    credit_flags.set_flags(UNIFIED)
    proxy = _RecordingGates(GATES)
    monkeypatch.setattr(liquidity, "GATES", proxy)
    async with async_session_maker() as s:
        await liquidity.fund_pair(s, pid, Decimal("10"), Decimal("20"), operator_user_id=1)
    p = await _pair(pid)
    assert p.pool_version == 2
    assert p.gold_reserve == Decimal("110.000000") and p.foreign_reserve == Decimal("120.000000")
    assert proxy.calls == [((GroupKey("fx", pid),), ())]  # pair 独占门闩

    async with async_session_maker() as s:
        await liquidity.withdraw_pair(s, pid, Decimal("10"), Decimal("20"), operator_user_id=1)
    p = await _pair(pid)
    assert p.pool_version == 3
    assert p.gold_reserve == Decimal("100.000000") and p.foreign_reserve == Decimal("100.000000")

    # 审计守恒：before/after 快照必须与金额一致（不因版本改动而变）
    async with async_session_maker() as s:
        evs = (await s.execute(
            select(AuditEvent).where(AuditEvent.event_type.in_(("fx_fund", "fx_withdraw")))
        )).scalars().all()
    assert len(evs) == 2
    fund = next(e for e in evs if e.event_type == "fx_fund")
    assert fund.payload["pool_before"] == {"gold": "100.000000", "foreign": "100.000000"}
    assert fund.payload["pool_after"] == {"gold": "110.000000", "foreign": "120.000000"}
    assert fund.payload["treasury_after"] == {"gold": "110.000000", "foreign": "120.000000"}


async def test_flag_off_fund_does_not_bump_pool_version():
    pid = await _seed_pair(code="LEGACY")
    async with async_session_maker() as s:
        await liquidity.fund_pair(s, pid, Decimal("10"), Decimal("20"), operator_user_id=1)
    p = await _pair(pid)
    assert p.pool_version == 1
    assert p.gold_reserve == Decimal("110.000000") and p.foreign_reserve == Decimal("120.000000")


async def test_unified_fund_requires_write_ownership(monkeypatch):
    from app.services.credit.ownership import EconomicWritesDisabled

    pid = await _seed_pair(code="OWN")
    credit_flags.set_flags(UNIFIED)
    monkeypatch.setattr(liquidity, "OWNERSHIP", WriteOwnership(url=SQLITE_URL))  # 未 acquire
    async with async_session_maker() as s:
        with pytest.raises(EconomicWritesDisabled):
            await liquidity.fund_pair(s, pid, Decimal("1"), Decimal("1"), operator_user_id=1)
    p = await _pair(pid)
    assert p.pool_version == 1 and p.gold_reserve == Decimal("100.000000")


# ────────────────────────── engine tick ──────────────────────────








# ────────────────────────── event first reaction ──────────────────────────








async def test_fund_pair_refreshes_retained_stale_treasury(writes_enabled):
    pid = await _seed_pair()
    credit_flags.set_flags(UNIFIED)
    async with async_session_maker() as s:
        stale = (await s.execute(select(FxTreasury).where(FxTreasury.pair_id == pid))).scalar_one()
        await s.commit()
        async with async_session_maker() as other:
            current = (await other.execute(select(FxTreasury).where(FxTreasury.pair_id == pid))).scalar_one()
            current.gold_balance = Decimal("40")
            current.foreign_balance = Decimal("60")
            await other.commit()
        assert stale.gold_balance == Decimal("100")
        await liquidity.fund_pair(s, pid, Decimal("10"), Decimal("20"), 1)
        assert stale.gold_balance == Decimal("50")
        assert stale.foreign_balance == Decimal("80")
    async with async_session_maker() as s:
        current = (await s.execute(select(FxTreasury).where(FxTreasury.pair_id == pid))).scalar_one()
        assert current.gold_balance == Decimal("50")
        assert current.foreign_balance == Decimal("80")
