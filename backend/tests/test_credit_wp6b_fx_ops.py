"""WP6b：FX 池子注撤资与系统 tick 的统一信贷接入。

覆盖：fund/withdraw bump `pool_version` + pair 独占门闩；tick 每 pair 独占门闩、
真实改池 bump 版本、reduce_only 禁止系统干预；event 首轮冲击在 halted/reduce_only
pair 上被拒绝；flag OFF 旧行为不变（不 bump、不拦）。
"""
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
from app.models.fx import FxEvent, FxPair, FxTrade, FxTreasury
from app.services import site_config
from app.services.credit import flags as credit_flags
from app.services.credit.flags import CreditFlags
from app.services.credit.gates import GATES
from app.services.credit.keys import GroupKey
from app.services.credit.ownership import WriteOwnership
from app.services.fx import engine as engine_mod
from app.services.fx import scheduler

pytestmark = pytest.mark.asyncio

ZERO = Decimal("0")
SQLITE_URL = "sqlite+aiosqlite:////dev/shm/credit-wp6b.db"
UNIFIED = CreditFlags(
    unified_credit_enabled=True,
    credit_leverage=Decimal("4"),
    credit_maintenance_ratio=Decimal("0.1"),
)
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


class _FixedRng:
    """确定性 RNG：正步长把 target 推向上界，噪声窗口推到极远。"""

    def gauss(self, mu, sigma):
        return 1.0

    def expovariate(self, lambd):
        return 1e9

    def random(self):
        return 1.0


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
    monkeypatch.setattr(engine_mod, "OWNERSHIP", own)
    monkeypatch.setattr(scheduler, "OWNERSHIP", own)
    yield own
    await own.release()


@pytest.fixture(autouse=True)
def _no_publish(monkeypatch):
    async def _noop(_trade):
        return None
    monkeypatch.setattr("app.services.fx.market_data.publish_trade", _noop)


async def _seed_pair(*, code: str = "TST", status: str = "trading", reduce_only: bool = False,
                     gold: str = "100", foreign: str = "100", target: str = "1",
                     initial: str = "1", target_min: str = "0.5", target_max: str = "2",
                     fx_enabled: bool = True) -> int:
    async with async_session_maker() as s:
        if fx_enabled:
            s.add(SiteConfig(key="fx_enabled", value="true", value_type="bool"))
        p = FxPair(
            currency_code=f"{code}{uuid.uuid4().hex[:4]}", currency_name=code,
            status=status, reduce_only=reduce_only,
            gold_reserve=Decimal(gold), foreign_reserve=Decimal(foreign),
            target_price=Decimal(target), initial_price=Decimal(initial),
            target_min=Decimal(target_min), target_max=Decimal(target_max),
        )
        s.add(p)
        await s.flush()
        s.add(FxTreasury(pair_id=p.id, gold_balance=Decimal(gold), foreign_balance=Decimal(foreign)))
        await s.commit()
        return int(p.id)


async def _pair(pid: int) -> FxPair:
    async with async_session_maker() as s:
        return (await s.execute(select(FxPair).where(FxPair.id == pid))).scalars().one()


async def _seed_event(pid: int, *, status: str = "draft") -> int:
    async with async_session_maker() as s:
        e = FxEvent(pair_id=pid, title="shock", kind="macro", status=status,
                    shock_ratio=Decimal("0.001"), first_reaction_ratio=Decimal("0.25"),
                    window_sec=600, budget=Decimal("10"))
        s.add(e)
        await s.commit()
        return int(e.id)


# ────────────────────────── fund / withdraw ──────────────────────────


async def test_unified_fund_and_withdraw_bump_pool_version(writes_enabled, monkeypatch):
    pid = await _seed_pair(code="FUND")
    credit_flags.set_flags(UNIFIED)
    proxy = _RecordingGates(GATES)
    monkeypatch.setattr(scheduler, "GATES", proxy)
    async with async_session_maker() as s:
        await scheduler.fund_pair(s, pid, Decimal("10"), Decimal("20"), operator_user_id=1)
    p = await _pair(pid)
    assert p.pool_version == 2
    assert p.gold_reserve == Decimal("110.000000") and p.foreign_reserve == Decimal("120.000000")
    assert proxy.calls == [((GroupKey("fx", pid),), ())]  # pair 独占门闩

    async with async_session_maker() as s:
        await scheduler.withdraw_pair(s, pid, Decimal("10"), Decimal("20"), operator_user_id=1)
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
        await scheduler.fund_pair(s, pid, Decimal("10"), Decimal("20"), operator_user_id=1)
    p = await _pair(pid)
    assert p.pool_version == 1
    assert p.gold_reserve == Decimal("110.000000") and p.foreign_reserve == Decimal("120.000000")


async def test_unified_fund_requires_write_ownership(monkeypatch):
    from app.services.credit.ownership import EconomicWritesDisabled

    pid = await _seed_pair(code="OWN")
    credit_flags.set_flags(UNIFIED)
    monkeypatch.setattr(scheduler, "OWNERSHIP", WriteOwnership(url=SQLITE_URL))  # 未 acquire
    async with async_session_maker() as s:
        with pytest.raises(EconomicWritesDisabled):
            await scheduler.fund_pair(s, pid, Decimal("1"), Decimal("1"), operator_user_id=1)
    p = await _pair(pid)
    assert p.pool_version == 1 and p.gold_reserve == Decimal("100.000000")


# ────────────────────────── engine tick ──────────────────────────


async def test_unified_tick_uses_exclusive_gate_and_bumps_pool_version(writes_enabled, monkeypatch):
    pid = await _seed_pair(code="TICK", target="1")
    credit_flags.set_flags(UNIFIED)
    engine = engine_mod.FxEngine(session_factory=async_session_maker)
    engine._last_target_at[pid] = NOW - timedelta(hours=2)
    proxy = _RecordingGates(GATES)
    monkeypatch.setattr(engine_mod, "GATES", proxy)

    result = await engine.tick(NOW, rng=_FixedRng())
    assert result.pairs == 1
    assert any(GroupKey("fx", pid) in exclusive for exclusive, _ in proxy.calls)
    p = await _pair(pid)
    assert p.pool_version == 2
    assert (p.gold_reserve, p.foreign_reserve) != (Decimal("100"), Decimal("100"))
    async with async_session_maker() as s:
        trades = (await s.execute(select(FxTrade).where(FxTrade.pair_id == pid))).scalars().all()
    assert any(t.source == "system_target" for t in trades)


async def test_unified_tick_skips_reduce_only_pair(writes_enabled):
    pid = await _seed_pair(code="RO", target="1", reduce_only=True)
    credit_flags.set_flags(UNIFIED)
    before = await _pair(pid)
    engine = engine_mod.FxEngine(session_factory=async_session_maker)
    engine._last_target_at[pid] = NOW - timedelta(hours=2)

    result = await engine.tick(NOW, rng=_FixedRng())
    assert result.pairs == 1 and result.skipped == 1
    assert any("reduce_only" in reason for reason in result.reasons)
    p = await _pair(pid)
    assert p.pool_version == before.pool_version
    assert p.target_price == before.target_price
    assert (p.gold_reserve, p.foreign_reserve) == (before.gold_reserve, before.foreign_reserve)
    async with async_session_maker() as s:
        assert (await s.execute(select(FxTrade).where(FxTrade.pair_id == pid))).scalars().all() == []


async def test_flag_off_tick_ignores_reduce_only_guard(writes_enabled):
    """flag OFF：旧行为不因 reduce_only 而跳过系统 tick（改动只在 ON 生效）。"""
    pid = await _seed_pair(code="ROOFF", target="1", reduce_only=True)
    before = await _pair(pid)
    engine = engine_mod.FxEngine(session_factory=async_session_maker)
    engine._last_target_at[pid] = NOW - timedelta(hours=2)

    result = await engine.tick(NOW, rng=_FixedRng())
    assert result.skipped == 0
    p = await _pair(pid)
    assert p.target_price != before.target_price  # legacy 仍走随机游走
    assert p.pool_version == before.pool_version + 1


# ────────────────────────── event first reaction ──────────────────────────


async def test_unified_publish_event_rejected_on_reduce_only_pair(writes_enabled):
    pid = await _seed_pair(code="EVRO", reduce_only=True)
    eid = await _seed_event(pid)
    credit_flags.set_flags(UNIFIED)
    async with async_session_maker() as s:
        with pytest.raises(HTTPException) as exc:
            await scheduler.publish_event(s, eid, NOW)
        await s.rollback()
    assert exc.value.status_code == 409
    async with async_session_maker() as s:
        e = (await s.execute(select(FxEvent).where(FxEvent.id == eid))).scalars().one()
        assert e.status == "draft"
        assert (await s.execute(select(FxTrade).where(FxTrade.pair_id == pid))).scalars().all() == []
    assert (await _pair(pid)).pool_version == 1


async def test_unified_publish_event_rejected_on_paused_pair(writes_enabled):
    pid = await _seed_pair(code="EVPAUSE", status="paused")
    eid = await _seed_event(pid)
    credit_flags.set_flags(UNIFIED)
    async with async_session_maker() as s:
        with pytest.raises(HTTPException) as exc:
            await scheduler.publish_event(s, eid, NOW)
        await s.rollback()
    assert exc.value.status_code == 409
    async with async_session_maker() as s:
        e = (await s.execute(select(FxEvent).where(FxEvent.id == eid))).scalars().one()
        assert e.status == "draft"


async def test_flag_off_publish_event_on_paused_pair_keeps_legacy_behavior():
    """flag OFF：legacy 路径不检查 pair 状态（历史行为），WP6b 不在 OFF 下改变它。"""
    pid = await _seed_pair(code="EVPAUSEOFF", status="paused")
    eid = await _seed_event(pid)
    async with async_session_maker() as s:
        published = await scheduler.publish_event(s, eid, NOW)
    assert published.status == "published"
    assert (await _pair(pid)).pool_version == 2


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
        await scheduler.fund_pair(s, pid, Decimal("10"), Decimal("20"), 1)
        assert stale.gold_balance == Decimal("50")
        assert stale.foreign_balance == Decimal("80")
    async with async_session_maker() as s:
        current = (await s.execute(select(FxTreasury).where(FxTreasury.pair_id == pid))).scalar_one()
        assert current.gold_balance == Decimal("50")
        assert current.foreign_balance == Decimal("80")
