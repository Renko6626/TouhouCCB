import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest, pytest_asyncio, uuid
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from app.core.database import async_session_maker
from app.models.audit import AuditEvent
from app.models.base import User, SiteConfig
from app.models.fx import FxPair, FxShortPosition, FxTreasury
from app.services.fx.shorts import pending_short_debt
from app.services.loan_sweep import run_sweep_once
from sqlalchemy import select

RATE = Decimal("0.01")


def _as_utc(dt: datetime) -> datetime:
    """SQLite drops tzinfo on read; interpret naive values as UTC. No tolerance."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _parse_clock(raw) -> datetime:
    assert isinstance(raw, str), f"authoritative clock must be an ISO string, got {raw!r}"
    return _as_utc(datetime.fromisoformat(raw))


@pytest_asyncio.fixture(autouse=True)
async def _seed_loan_rate(setup_db):
    """conftest 的 setup_db 负责清库；此 fixture 仅追加 loan_daily_rate 种子。"""
    async with async_session_maker() as s:
        async with s.begin():
            s.add(SiteConfig(key="loan_daily_rate", value="0.01", value_type="decimal"))


async def _seed_user(debt, last_accrued):
    async with async_session_maker() as s:
        async with s.begin():
            u = User(
                username=f"u_{uuid.uuid4().hex[:6]}",
                email=f"{uuid.uuid4().hex[:6]}@t.com",
                casdoor_id=uuid.uuid4().hex,
                cash=Decimal("0"),
                debt=Decimal(debt),
                debt_last_accrued_at=last_accrued,
            )
            s.add(u)
            await s.flush()
            return u.id


@pytest.mark.asyncio
async def test_sweep_skips_users_with_zero_debt():
    uid = await _seed_user(debt="0", last_accrued=None)
    await run_sweep_once()
    async with async_session_maker() as s:
        u = await s.get(User, uid)
    assert u.debt == Decimal("0")


@pytest.mark.asyncio
async def test_sweep_accrues_interest():
    now = datetime.now(timezone.utc)
    uid = await _seed_user(debt="1000", last_accrued=now - timedelta(hours=1))
    await run_sweep_once()
    async with async_session_maker() as s:
        u = await s.get(User, uid)
    # 1h @ 1%/day ≈ 1000 * (1 + 0.01/24) ≈ 1000.4167
    assert u.debt > Decimal("1000.3")
    assert u.debt < Decimal("1000.5")
    assert u.debt_last_accrued_at is not None
    assert u.economic_version == 1


@pytest.mark.asyncio
async def test_sweep_multiple_users_independent():
    now = datetime.now(timezone.utc)
    uid1 = await _seed_user(debt="100", last_accrued=now - timedelta(days=1))
    uid2 = await _seed_user(debt="0", last_accrued=None)
    await run_sweep_once()
    async with async_session_maker() as s:
        u1 = await s.get(User, uid1)
        u2 = await s.get(User, uid2)
    assert u1.debt > Decimal("100.5") and u1.debt < Decimal("101.5")
    assert u2.debt == Decimal("0")
    assert u1.economic_version == 1
    assert u2.economic_version == 0


@pytest.mark.asyncio
async def test_sweep_skips_recently_accrued_users():
    """审计 M1：距上次结息不足 loan_sweep_min_accrual_sec（默认 3600）的用户本 tick 跳过，
    不写 interest_accrual 事件；超过窗口的照常结息。"""
    from app.models.audit import AuditEvent
    now = datetime.now(timezone.utc)
    recent = await _seed_user(debt="1000", last_accrued=now - timedelta(minutes=5))
    stale = await _seed_user(debt="1000", last_accrued=now - timedelta(hours=2))
    touched = await run_sweep_once()
    assert touched == 1
    async with async_session_maker() as s:
        assert (await s.get(User, recent)).debt == Decimal("1000")
        assert (await s.get(User, stale)).debt > Decimal("1000")
        evs = (await s.execute(select(AuditEvent).where(AuditEvent.event_type == "interest_accrual"))).scalars().all()
        assert [e.user_id for e in evs] == [stale]


async def _seed_short_user(*, principal="100", interest="0", accrued, cash="0", debt="0",
                           gold="1000", foreign="1000", restricted="50", proceeds="50",
                           gold_accrued=None):
    """Seed a user holding a positive foreign short plus untouched pool/treasury."""
    tag = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(
                username=f"short_{tag}",
                email=f"{tag}@t.com",
                casdoor_id=uuid.uuid4().hex,
                cash=Decimal(cash),
                debt=Decimal(debt),
                debt_last_accrued_at=gold_accrued,
            )
            s.add(u)
            await s.flush()
            pair = FxPair(
                currency_code=f"FX_{tag}",
                currency_name="test",
                status="trading",
                gold_reserve=Decimal(gold),
                foreign_reserve=Decimal(foreign),
            )
            s.add(pair)
            await s.flush()
            s.add(FxTreasury(pair_id=pair.id, gold_balance=Decimal("777"), foreign_balance=Decimal("888")))
            pos = FxShortPosition(
                user_id=u.id,
                pair_id=pair.id,
                principal_foreign=Decimal(principal),
                interest_foreign=Decimal(interest),
                restricted_gold=Decimal(restricted),
                proceeds_basis_gold=Decimal(proceeds),
                interest_last_accrued_at=accrued,
            )
            s.add(pos)
            await s.flush()
            return u.id, pair.id, pos.id


@pytest.mark.asyncio
async def test_sweep_accrues_foreign_interest_only_and_is_idempotent():
    """WP2c: a User.debt=0 user with a positive short principal is swept.

    One write persists foreign interest, advances the shared user economic
    version and appends exactly one audit event; an immediate second tick is a
    min-gap no-op. Pool reserves, treasury actual stock, cash and gold debt must
    not move, and the read-time pending debt at the persisted clock T must equal
    the persisted amount (write/read same source, independent of the sweep).
    """
    now = datetime.now(timezone.utc)
    uid, pid, sid = await _seed_short_user(principal="100", accrued=now - timedelta(hours=2))

    # Read-time obligation already carries the unpersisted two hours of interest.
    async with async_session_maker() as s:
        seeded = await s.get(FxShortPosition, sid)
        read_before = pending_short_debt(seeded, RATE, now)
        clock_before = seeded.interest_last_accrued_at
    assert read_before > seeded.principal_foreign + seeded.interest_foreign

    touched = await run_sweep_once()
    assert touched == 1

    async with async_session_maker() as s:
        u = await s.get(User, uid)
        pos = await s.get(FxShortPosition, sid)
        pair = await s.get(FxPair, pid)
        treasury = (await s.execute(
            select(FxTreasury).where(FxTreasury.pair_id == pid)
        )).scalar_one()
        events = (await s.execute(
            select(AuditEvent).where(AuditEvent.user_id == uid)
        )).scalars().all()

    persisted = pos.principal_foreign + pos.interest_foreign
    assert pos.principal_foreign == Decimal("100")
    assert pos.interest_foreign > Decimal("0")
    assert pos.interest_last_accrued_at is not None
    # Read/write same source: at the persisted clock the pending read equals what was stored.
    assert pending_short_debt(pos, RATE, pos.interest_last_accrued_at) == persisted
    # Only foreign interest / version / audit move.
    assert u.debt == Decimal("0")
    assert u.cash == Decimal("0")
    assert u.economic_version == 1
    assert pair.gold_reserve == Decimal("1000")
    assert pair.foreign_reserve == Decimal("1000")
    assert pair.pool_version == 1
    assert treasury.gold_balance == Decimal("777")
    assert treasury.foreign_balance == Decimal("888")
    assert len(events) == 1
    event = events[0]
    assert event.event_type == "interest_accrual"
    assert event.payload["currency"] == "foreign"
    assert event.payload["pair_id"] == pid
    assert Decimal(event.payload["interest_foreign_delta"]) == pos.interest_foreign
    assert Decimal(event.payload["interest"]) == Decimal("0")

    # WP5 replay needs the exact foreign-interest clock, not a later wall clock
    # or a drifting float. The payload must carry the row's before/after clocks
    # and the very T used to accrue; AuditEvent.ts must be that same T.
    assert _parse_clock(event.payload["interest_last_accrued_at_before"]) == _as_utc(clock_before)
    assert _parse_clock(event.payload["interest_last_accrued_at_after"]) == _as_utc(pos.interest_last_accrued_at)
    assert _parse_clock(event.payload["accrued_at"]) == _as_utc(pos.interest_last_accrued_at)
    assert _as_utc(event.ts) == _as_utc(pos.interest_last_accrued_at)
    assert pos.interest_last_accrued_at > clock_before

    # Immediate second tick: still inside loan_sweep_min_accrual_sec -> no-op.
    assert await run_sweep_once() == 0
    async with async_session_maker() as s:
        pos2 = await s.get(FxShortPosition, sid)
        u2 = await s.get(User, uid)
        events2 = (await s.execute(
            select(AuditEvent).where(AuditEvent.user_id == uid)
        )).scalars().all()
    assert pos2.interest_foreign == pos.interest_foreign
    assert pos2.interest_last_accrued_at == pos.interest_last_accrued_at
    assert u2.economic_version == 1
    assert len(events2) == 1


@pytest.mark.asyncio
async def test_sweep_accrues_foreign_even_when_gold_clock_is_recent():
    """A recent gold clock must not skip this user's foreign short rows.

    The old sweep `continue`d the whole user on the loan min-gap check, which
    would silently starve foreign interest whenever a user also had recent gold
    debt. Foreign rows use their own clock; this user's gold debt stays fixed.
    """
    now = datetime.now(timezone.utc)
    uid, _pid, sid = await _seed_short_user(
        principal="100",
        accrued=now - timedelta(hours=2),
        debt="500",
        gold_accrued=now - timedelta(minutes=5),
    )
    assert await run_sweep_once() == 1
    async with async_session_maker() as s:
        u = await s.get(User, uid)
        pos = await s.get(FxShortPosition, sid)
    assert u.debt == Decimal("500")          # recent gold leg skipped
    assert pos.interest_foreign > Decimal("0")
    assert u.economic_version == 1
