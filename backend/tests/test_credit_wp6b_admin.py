"""WP6b：管理员资金/贷款入口的统一信贷准入（flag=unified_credit_enabled）。

覆盖：扣款后 E 检查、冻结期充值放行、强制放贷的初始门槛、坏账核销显式越权、
批量逐用户短事务与跳过、版本自增、门闩先于 user 行锁、配置非法 fail-closed，
以及 flag OFF 的旧行为逐字段不变。
"""
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.database import async_session_maker
from app.models.base import Market, Outcome, Position, SiteConfig, User
from app.models.fx import FxPair, FxShortPosition
from app.models.ledger import LedgerEntry
from app.services import admin_user_service as svc
from app.services.credit import flags as credit_flags
from app.services.credit.flags import CreditFlags
from app.services.credit.gates import GATES
from app.services.credit.keys import GroupKey
from app.services.credit.ownership import WriteOwnership
from app.services.credit.version import bump_economic_version, economic_version_of

pytestmark = pytest.mark.asyncio

ZERO = Decimal("0")
SQLITE_URL = "sqlite+aiosqlite:////dev/shm/credit-wp6b.db"

# leverage=4 → R_initial=1/3；maintenance=0.1
UNIFIED = CreditFlags(
    unified_credit_enabled=True,
    credit_leverage=Decimal("4"),
    credit_maintenance_ratio=Decimal("0.1"),
)


@pytest.fixture(autouse=True)
def _clean_flags():
    credit_flags.clear_flags()
    yield
    credit_flags.clear_flags()
    credit_flags.set_new_risk_frozen(None)


@pytest_asyncio.fixture
async def writes_enabled(monkeypatch):
    """统一模式下 require_writes() 必须通过：用独立实例替换模块内单例。"""
    own = WriteOwnership(url=SQLITE_URL)
    await own.acquire()
    monkeypatch.setattr(svc, "OWNERSHIP", own)
    yield own
    await own.release()


async def _seed_user(
    *, cash: Decimal, debt: Decimal = ZERO, frozen: bool = False,
    is_superuser: bool = False,
) -> int:
    async with async_session_maker() as s:
        u = User(
            username=f"u_{uuid.uuid4().hex[:8]}",
            email=f"{uuid.uuid4().hex[:8]}@t.com",
            casdoor_id=uuid.uuid4().hex,
            cash=cash,
            debt=debt,
            credit_frozen=frozen,
            is_superuser=is_superuser,
            is_active=True,
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return int(u.id)


async def _seed_admin() -> int:
    return await _seed_user(cash=ZERO, is_superuser=True)


async def _state(uid: int) -> tuple[Decimal, Decimal, int]:
    async with async_session_maker() as s:
        u = (await s.execute(select(User).where(User.id == uid))).scalars().one()
        return Decimal(u.cash), Decimal(u.debt), economic_version_of(u)


async def _seed_market_position(uid: int, *, amount: str = "10") -> GroupKey:
    async with async_session_maker() as s:
        m = Market(title=f"m_{uuid.uuid4().hex[:6]}", liquidity_b=100.0, tags="")
        s.add(m)
        await s.flush()
        o = Outcome(market_id=m.id, label="A", total_shares=Decimal("0"))
        s.add(o)
        await s.flush()
        s.add(Position(user_id=uid, outcome_id=o.id, amount=Decimal(amount),
                       cost_basis=Decimal("1")))
        await s.commit()
        return GroupKey("lmsr", int(m.id))


async def _seed_short_pairs(
    uid: int, *, pair_status: str = "trading",
    locks: tuple[str, ...] = ("50", "30"), principal: str = "10",
) -> list[GroupKey]:
    """Persist foreign-only short debt (User.debt stays 0) on real pairs."""
    keys: list[GroupKey] = []
    async with async_session_maker() as s:
        for lock in locks:
            pair = FxPair(
                currency_code=uuid.uuid4().hex[:16], currency_name="test",
                status=pair_status, gold_reserve=Decimal("10000"),
                foreign_reserve=Decimal("10000"),
            )
            s.add(pair)
            await s.flush()
            s.add(FxShortPosition(
                user_id=uid, pair_id=pair.id, principal_foreign=Decimal(principal),
                restricted_gold=Decimal(lock),
                interest_last_accrued_at=datetime.now(timezone.utc),
            ))
            keys.append(GroupKey("fx", int(pair.id)))
        await s.commit()
    return keys


# ────────────────────────── 单用户 adjust_cash ──────────────────────────


async def test_unified_deduction_below_initial_margin_rejected(writes_enabled):
    admin = await _seed_admin()
    uid = await _seed_user(cash=Decimal("100"), debt=Decimal("900"))
    credit_flags.set_flags(UNIFIED)
    async with async_session_maker() as s:
        with pytest.raises(svc.AdminUserError) as exc:
            await svc.adjust_cash(s, target_id=uid, amount=Decimal("-50"), reason="pull", admin_id=admin)
    assert exc.value.status == 409
    assert "初始保证金" in exc.value.detail
    cash, debt, version = await _state(uid)
    assert (cash, debt, version) == (Decimal("100.000000"), Decimal("900.000000"), 0)


async def test_unified_grant_allowed_for_frozen_user_and_bumps_version(writes_enabled):
    admin = await _seed_admin()
    uid = await _seed_user(cash=Decimal("100"), debt=Decimal("900"), frozen=True)
    credit_flags.set_flags(UNIFIED)
    async with async_session_maker() as s:
        r = await svc.adjust_cash(s, target_id=uid, amount=Decimal("50"), reason="topup", admin_id=admin)
    assert r["new_cash"] == 150.0
    cash, debt, version = await _state(uid)
    assert cash == Decimal("150.000000") and debt == Decimal("900.000000")
    assert version == 1


async def test_unified_deduction_succeeds_when_margin_ok_and_bumps_version(writes_enabled):
    admin = await _seed_admin()
    uid = await _seed_user(cash=Decimal("10000"), debt=Decimal("100"))
    credit_flags.set_flags(UNIFIED)
    async with async_session_maker() as s:
        await svc.adjust_cash(s, target_id=uid, amount=Decimal("-100"), reason="fine", admin_id=admin)
    cash, _, version = await _state(uid)
    assert cash == Decimal("9900.000000") and version == 1


async def test_flag_off_keeps_legacy_deduction_and_does_not_bump_version():
    admin = await _seed_admin()
    uid = await _seed_user(cash=Decimal("100"), debt=Decimal("900"))
    async with async_session_maker() as s:
        r = await svc.adjust_cash(s, target_id=uid, amount=Decimal("-50"), reason="legacy", admin_id=admin)
    assert r["new_cash"] == 50.0
    cash, _, version = await _state(uid)
    assert cash == Decimal("50.000000") and version == 0


async def test_unified_missing_thresholds_fails_closed(writes_enabled):
    admin = await _seed_admin()
    uid = await _seed_user(cash=Decimal("100"))
    credit_flags.set_flags(CreditFlags(unified_credit_enabled=True))  # 无门槛配置
    async with async_session_maker() as s:
        with pytest.raises(svc.AdminUserError) as exc:
            await svc.adjust_cash(s, target_id=uid, amount=Decimal("-1"), reason="x", admin_id=admin)
    assert exc.value.status == 503
    cash, _, version = await _state(uid)
    assert cash == Decimal("100.000000") and version == 0


async def test_flag_off_readonly_still_rejects_cash_write(monkeypatch):
    from app.services.credit.ownership import EconomicWritesDisabled
    owner = WriteOwnership(url=SQLITE_URL)
    owner.mark_read_only()
    monkeypatch.setattr(svc, "OWNERSHIP", owner)
    async with async_session_maker() as s:
        with pytest.raises(EconomicWritesDisabled):
            await svc.adjust_cash(s, target_id=1, amount=Decimal("1"), reason="test", admin_id=1)


async def test_unified_admin_write_requires_write_ownership(monkeypatch):
    from app.services.credit.ownership import EconomicWritesDisabled

    admin = await _seed_admin()
    uid = await _seed_user(cash=Decimal("100"))
    credit_flags.set_flags(UNIFIED)
    monkeypatch.setattr(svc, "OWNERSHIP", WriteOwnership(url=SQLITE_URL))  # 未 acquire
    async with async_session_maker() as s:
        with pytest.raises(EconomicWritesDisabled):
            await svc.adjust_cash(s, target_id=uid, amount=Decimal("10"), reason="x", admin_id=admin)
    cash, _, version = await _state(uid)
    assert cash == Decimal("100.000000") and version == 0


async def test_unified_deduction_holds_collateral_gate_before_user_lock(writes_enabled, monkeypatch):
    """门闩必须在 user 行锁之前拿到（门闩批次已持有才允许行锁）。"""
    admin = await _seed_admin()
    uid = await _seed_user(cash=Decimal("100"), debt=Decimal("900"))
    key = await _seed_market_position(uid)
    credit_flags.set_flags(UNIFIED)

    observed: list[frozenset] = []
    original = svc._lock_user

    async def spy(db, user_id):
        observed.append(GATES.held_keys_by_current_task())
        return await original(db, user_id)

    monkeypatch.setattr(svc, "_lock_user", spy)
    async with async_session_maker() as s:
        with pytest.raises(svc.AdminUserError):
            await svc.adjust_cash(s, target_id=uid, amount=Decimal("-50"), reason="gate", admin_id=admin)
    assert observed and key in observed[0]
    assert GATES.metrics().holders == 0  # 释放干净


async def test_unified_debit_retries_after_short_appears_post_discovery(writes_enabled, monkeypatch):
    """Discovery missed a concurrently-opened short: release gates, rediscover, retry once.

    The first discovery sees no debt and holds no gates. The injected write opens a
    real trading short and bumps the version before the User lock; the admin path
    must not quote the unseen pair under a missing gate — it releases everything,
    re-discovers the full dependency, and writes exactly once.
    """
    admin = await _seed_admin()
    uid = await _seed_user(cash=Decimal("100"))
    credit_flags.set_flags(UNIFIED)

    original = svc._lock_user
    injected = {"done": False}

    async def spy(db, user_id):
        if not injected["done"]:
            injected["done"] = True
            async with async_session_maker() as s2:
                pair = FxPair(
                    currency_code=uuid.uuid4().hex[:16], currency_name="test",
                    status="trading", gold_reserve=Decimal("10000"),
                    foreign_reserve=Decimal("10000"),
                )
                s2.add(pair)
                await s2.flush()
                s2.add(FxShortPosition(
                    user_id=user_id, pair_id=pair.id,
                    principal_foreign=Decimal("10"), restricted_gold=Decimal("20"),
                    interest_last_accrued_at=datetime.now(timezone.utc),
                ))
                bumped = await s2.get(User, user_id)
                bump_economic_version(bumped)
                await s2.commit()
        return await original(db, user_id)

    monkeypatch.setattr(svc, "_lock_user", spy)
    async with async_session_maker() as s:
        r = await svc.adjust_cash(s, target_id=uid, amount=Decimal("-10"),
                                  reason="retry", admin_id=admin)
    assert r["new_cash"] == 90.0
    cash, _, version = await _state(uid)
    assert cash == Decimal("90.000000") and version == 2
    async with async_session_maker() as s:
        entries = (await s.execute(
            select(LedgerEntry).where(LedgerEntry.user_id == uid)
        )).scalars().all()
    assert len(entries) == 1
    assert entries[0].cash_delta == Decimal("-10.000000")
    assert GATES.metrics().holders == 0


# ────────────────────────── force_loan / forgive_debt ──────────────────────────


async def _seed_loan_config() -> None:
    async with async_session_maker() as s:
        s.add_all([
            SiteConfig(key="loan_enabled", value="true", value_type="bool"),
            SiteConfig(key="loan_daily_rate", value="0.01", value_type="decimal"),
        ])
        await s.commit()


async def test_unified_force_loan_rejected_for_frozen_user(writes_enabled):
    await _seed_loan_config()
    admin = await _seed_admin()
    uid = await _seed_user(cash=Decimal("1000"), frozen=True)
    credit_flags.set_flags(UNIFIED)
    async with async_session_maker() as s:
        with pytest.raises(svc.AdminUserError) as exc:
            await svc.force_loan(s, target_id=uid, amount=Decimal("100"), reason="x", admin_id=admin)
    assert exc.value.status == 409 and "冻结" in exc.value.detail
    cash, debt, version = await _state(uid)
    assert (cash, debt, version) == (Decimal("1000.000000"), ZERO, 0)


async def test_unified_force_loan_below_initial_margin_rejected(writes_enabled):
    await _seed_loan_config()
    admin = await _seed_admin()
    uid = await _seed_user(cash=Decimal("10"), debt=Decimal("1000"))
    credit_flags.set_flags(UNIFIED)
    async with async_session_maker() as s:
        with pytest.raises(svc.AdminUserError) as exc:
            await svc.force_loan(s, target_id=uid, amount=Decimal("100"), reason="x", admin_id=admin)
    assert exc.value.status == 409 and "初始保证金" in exc.value.detail
    _, debt, version = await _state(uid)
    assert debt == Decimal("1000.000000") and version == 0


async def test_unified_force_loan_success_bumps_version_and_audits(writes_enabled):
    await _seed_loan_config()
    admin = await _seed_admin()
    uid = await _seed_user(cash=Decimal("1000"))
    credit_flags.set_flags(UNIFIED)
    async with async_session_maker() as s:
        r = await svc.force_loan(s, target_id=uid, amount=Decimal("100"), reason="operator grant", admin_id=admin)
    assert r["cash"] == 1100.0 and r["debt"] == 100.0
    cash, debt, version = await _state(uid)
    assert cash == Decimal("1100.000000") and debt == Decimal("100.000000")
    assert version == 1  # WP6a 若在 loan_service 内再 bump，这里只会更大，不影响单调语义

    from app.models.ledger import LedgerEntry
    async with async_session_maker() as s:
        entry = (await s.execute(
            select(LedgerEntry).where(LedgerEntry.user_id == uid)
        )).scalars().one()
        assert entry.entry_type == "admin_force_loan"
        assert entry.operator_user_id == admin
        assert entry.reason == "operator grant"


async def test_unified_forgive_debt_allowed_for_frozen_user(writes_enabled):
    await _seed_loan_config()
    admin = await _seed_admin()
    uid = await _seed_user(cash=Decimal("0"), debt=Decimal("100"), frozen=True)
    credit_flags.set_flags(UNIFIED)
    async with async_session_maker() as s:
        r = await svc.forgive_debt(s, target_id=uid, amount=Decimal("100"), reason="writeoff", admin_id=admin)
    assert r["effective"] == 100.0
    cash, debt, version = await _state(uid)
    assert debt == ZERO and version == 1


async def test_flag_off_force_loan_keeps_economic_behavior():
    await _seed_loan_config()
    admin = await _seed_admin()
    uid = await _seed_user(cash=Decimal("10"), debt=Decimal("1000"))
    async with async_session_maker() as s:
        await svc.force_loan(s, target_id=uid, amount=Decimal("100"), reason="legacy", admin_id=admin)
    _, _, version = await _state(uid)
    assert version == 1  # centralized loan primitive advances metadata in both modes


# ────────────────────────── 批量 ──────────────────────────


async def test_unified_batch_deduction_skips_under_margin_user(writes_enabled):
    admin = await _seed_admin()
    healthy = await _seed_user(cash=Decimal("1000"))
    risky = await _seed_user(cash=Decimal("100"), debt=Decimal("900"))
    credit_flags.set_flags(UNIFIED)
    f = svc.UserFilter(user_id_min=min(healthy, risky), user_id_max=max(healthy, risky))
    async with async_session_maker() as s:
        r = await svc.batch_adjust_cash(
            s, f=f, amount=Decimal("-50"), reason="batch pull", admin_id=admin, dry_run=False,
        )
    assert r["updated_count"] == 1 and r["failed_count"] == 1
    assert r["updated"][0]["user_id"] == healthy
    assert "统一信贷拒绝" in r["failed"][0]["reason"]
    h_cash, _, h_version = await _state(healthy)
    r_cash, _, r_version = await _state(risky)
    assert h_cash == Decimal("950.000000") and h_version == 1
    assert r_cash == Decimal("100.000000") and r_version == 0


async def test_unified_batch_grant_applies_to_all_and_bumps(writes_enabled):
    admin = await _seed_admin()
    a = await _seed_user(cash=Decimal("100"))
    b = await _seed_user(cash=Decimal("100"))
    credit_flags.set_flags(UNIFIED)
    f = svc.UserFilter(user_id_min=min(a, b), user_id_max=max(a, b))
    async with async_session_maker() as s:
        r = await svc.batch_adjust_cash(
            s, f=f, amount=Decimal("25"), reason="batch grant", admin_id=admin, dry_run=False,
        )
    assert r["updated_count"] == 2 and r["failed_count"] == 0
    for uid in (a, b):
        cash, _, version = await _state(uid)
        assert cash == Decimal("125.000000") and version == 1


async def test_unified_batch_debit_foreign_only_debt_holds_gates_and_skips_below_lock(
    writes_enabled, monkeypatch,
):
    """D=0 foreign-only debt: full gates held, and C cannot fall below S."""
    admin = await _seed_admin()
    uid = await _seed_user(cash=Decimal("100"))
    keys = await _seed_short_pairs(uid)  # S = 80, User.debt = 0
    credit_flags.set_flags(UNIFIED)

    observed: list[frozenset] = []
    original = svc._lock_user

    async def spy(db, user_id):
        observed.append(GATES.held_keys_by_current_task())
        return await original(db, user_id)

    monkeypatch.setattr(svc, "_lock_user", spy)
    f = svc.UserFilter(user_id_min=uid, user_id_max=uid)
    async with async_session_maker() as s:
        r = await svc.batch_adjust_cash(
            s, f=f, amount=Decimal("-30"), reason="pull", admin_id=admin, dry_run=False,
        )
    assert r["updated_count"] == 0 and r["failed_count"] == 1
    assert "空头锁金" in r["failed"][0]["reason"]
    assert observed and set(keys).issubset(observed[0])
    assert GATES.metrics().holders == 0
    cash, _, version = await _state(uid)
    assert cash == Decimal("100.000000") and version == 0


async def test_unified_batch_debit_foreign_only_debt_prices_cover_cost(writes_enabled):
    """Free cash exactly at S is admitted only after pricing the foreign cover cost."""
    admin = await _seed_admin()
    uid = await _seed_user(cash=Decimal("100"))
    await _seed_short_pairs(uid)
    credit_flags.set_flags(UNIFIED)
    f = svc.UserFilter(user_id_min=uid, user_id_max=uid)
    async with async_session_maker() as s:
        r = await svc.batch_adjust_cash(
            s, f=f, amount=Decimal("-20"), reason="fee", admin_id=admin, dry_run=False,
        )
    assert r["updated_count"] == 1 and r["failed_count"] == 0
    cash, _, version = await _state(uid)
    assert cash == Decimal("80.000000") and version == 1


async def test_flag_off_batch_keeps_single_transaction_behavior():
    admin = await _seed_admin()
    a = await _seed_user(cash=Decimal("100"), debt=Decimal("900"))
    f = svc.UserFilter(user_id_min=a, user_id_max=a)
    async with async_session_maker() as s:
        r = await svc.batch_adjust_cash(
            s, f=f, amount=Decimal("-50"), reason="legacy batch", admin_id=admin, dry_run=False,
        )
    assert r["updated_count"] == 1 and r["failed_count"] == 0
    cash, _, version = await _state(a)
    assert cash == Decimal("50.000000") and version == 0


# ────────────────────────── amnesty ──────────────────────────


async def test_unified_amnesty_forgives_frozen_debt_as_explicit_writeoff(writes_enabled):
    await _seed_loan_config()
    admin = await _seed_admin()
    uid = await _seed_user(cash=Decimal("0"), debt=Decimal("100"), frozen=True)
    credit_flags.set_flags(UNIFIED)
    f = svc.UserFilter(user_id_min=uid, user_id_max=uid)
    async with async_session_maker() as s:
        r = await svc.amnesty(
            s, f=f, reset_cash_to=Decimal("500"), forgive_debt=True,
            reason="season amnesty", admin_id=admin, dry_run=False,
        )
    assert r["updated_count"] == 1 and r["failed_count"] == 0
    assert r["total_debt_forgiven"] == 100.0
    cash, debt, version = await _state(uid)
    assert cash == Decimal("500.000000") and debt == ZERO and version == 1
    async with async_session_maker() as s:
        u = (await s.execute(select(User).where(User.id == uid))).scalars().one()
        assert u.credit_frozen is True  # 大赦只核销债务，解冻仍是独立动作


async def test_unified_amnesty_skips_user_whose_margin_breaks(writes_enabled):
    await _seed_loan_config()
    admin = await _seed_admin()
    uid = await _seed_user(cash=Decimal("1000"), debt=Decimal("100"))
    credit_flags.set_flags(UNIFIED)
    f = svc.UserFilter(user_id_min=uid, user_id_max=uid)
    async with async_session_maker() as s:
        r = await svc.amnesty(
            s, f=f, reset_cash_to=Decimal("0"), forgive_debt=False,
            reason="reset down", admin_id=admin, dry_run=False,
        )
    assert r["updated_count"] == 0 and r["failed_count"] == 1
    assert "统一信贷拒绝" in r["failed"][0]["reason"]
    cash, debt, version = await _state(uid)
    assert cash == Decimal("1000.000000") and debt == Decimal("100.000000") and version == 0


async def test_unified_amnesty_foreign_only_debt_holds_gates_and_prices_cover(
    writes_enabled, monkeypatch,
):
    """Forgiving gold debt must not forgive foreign debt: full gates + K under lock.

    ``forgive_debt=True`` with User.debt==0 and a cover cost that breaks the initial
    margin has to be refused for that concrete reason, not skipped as a version
    conflict or admitted by the no-debt fast path.
    """
    await _seed_loan_config()
    admin = await _seed_admin()
    uid = await _seed_user(cash=Decimal("100"))
    keys = await _seed_short_pairs(uid, principal="9000")  # S = 80, K >> cash
    credit_flags.set_flags(UNIFIED)

    observed: list[frozenset] = []
    original = svc._lock_user

    async def spy(db, user_id):
        observed.append(GATES.held_keys_by_current_task())
        return await original(db, user_id)

    monkeypatch.setattr(svc, "_lock_user", spy)
    f = svc.UserFilter(user_id_min=uid, user_id_max=uid)
    async with async_session_maker() as s:
        r = await svc.amnesty(
            s, f=f, reset_cash_to=Decimal("80"), forgive_debt=True,
            reason="reset to lock boundary", admin_id=admin, dry_run=False,
        )
    assert r["updated_count"] == 0 and r["failed_count"] == 1
    assert "初始保证金" in r["failed"][0]["reason"]
    assert observed and set(keys).issubset(observed[0])
    assert GATES.metrics().holders == 0
    cash, debt, version = await _state(uid)
    assert cash == Decimal("100.000000") and debt == ZERO and version == 0


async def test_flag_off_amnesty_resets_without_version_bump():
    await _seed_loan_config()
    admin = await _seed_admin()
    uid = await _seed_user(cash=Decimal("1000"))
    f = svc.UserFilter(user_id_min=uid, user_id_max=uid)
    async with async_session_maker() as s:
        r = await svc.amnesty(
            s, f=f, reset_cash_to=Decimal("500"), forgive_debt=True,
            reason="legacy amnesty", admin_id=admin, dry_run=False,
        )
    assert r["updated_count"] == 1
    cash, _, version = await _state(uid)
    assert cash == Decimal("500.000000") and version == 0


# ────────────────────────── 审计重放一致性 ──────────────────────────


async def test_unified_admin_writes_keep_audit_replay_consistent(writes_enabled):
    """统一路径的 ledger 审计快照必须能被 replay fold + live 比对接受。"""
    from app.services import audit_replay

    await _seed_loan_config()
    admin = await _seed_admin()
    uid = await _seed_user(cash=Decimal("1000"))
    credit_flags.set_flags(UNIFIED)
    async with async_session_maker() as s:
        await svc.adjust_cash(s, target_id=uid, amount=Decimal("100"), reason="grant", admin_id=admin)
        await svc.adjust_cash(s, target_id=uid, amount=Decimal("-50"), reason="pull", admin_id=admin)
        await svc.force_loan(s, target_id=uid, amount=Decimal("200"), reason="loan", admin_id=admin)
        await svc.forgive_debt(s, target_id=uid, amount=Decimal("50"), reason="writeoff", admin_id=admin)
    async with async_session_maker() as s:
        events = await audit_replay.load_events(s)
        snap, mism = audit_replay.fold(events, check=True)
        live = await audit_replay.compare_with_live(s, snap)
    assert mism == [] and live == []
    cash, debt, version = await _state(uid)
    assert cash == Decimal("1250.000000") and debt == Decimal("150.000000") and version == 4


async def test_owner_loss_while_waiting_for_gate_rejects_deduction(writes_enabled, monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager
    from app.services.credit.ownership import EconomicWritesDisabled
    admin = await _seed_admin()
    uid = await _seed_user(cash=Decimal("1000"), debt=Decimal("10"))
    key = await _seed_market_position(uid)
    credit_flags.set_flags(UNIFIED)
    arrived = asyncio.Event()
    original = GATES.hold

    @asynccontextmanager
    async def observed_hold(**kwargs):
        arrived.set()
        async with original(**kwargs):
            yield

    async def request():
        async with async_session_maker() as s:
            return await svc.adjust_cash(s, target_id=uid, amount=Decimal("-1"), reason="race", admin_id=admin)

    async with original(exclusive=[key]):
        monkeypatch.setattr(GATES, "hold", observed_hold)
        task = asyncio.create_task(request())
        await asyncio.wait_for(arrived.wait(), 2)
        writes_enabled.mark_read_only()
    with pytest.raises(EconomicWritesDisabled):
        await task
    assert await _state(uid) == (Decimal("1000.000000"), Decimal("10.000000"), 0)
