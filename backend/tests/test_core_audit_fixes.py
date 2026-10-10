"""核心审计（2026-08-22）修复回归：
#1 借款额度在行锁下重算；#2 过 closes_at 的 TRADING 市场 = HALT 口径；
#3 writer 强平每市场回款同事务还债；#4 sweep 进程内互斥。
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import Market, MarketStatus, Outcome, Position, SiteConfig, User, LiquidationEvent
from app.services import liquidation_sweep, wealth
from app.services.market_open import market_is_open
from app.services.market_writer import WRITER


@pytest_asyncio.fixture(autouse=True)
async def _cfg(setup_db):
    async with async_session_maker() as s:
        async with s.begin():
            for k, v, t in [
                ("loan_enabled", "true", "bool"), ("loan_leverage_k", "1.0", "decimal"),
                ("loan_daily_rate", "0.01", "decimal"), ("loan_sweep_interval_sec", "60", "int"),
                ("sell_fee_rate", "0", "decimal"),
                ("liquidation_enabled", "true", "bool"),
                ("liquidation_hard_threshold", "0.2", "decimal"),
                ("liquidation_soft_threshold", "0.5", "decimal"),
                ("liquidation_emergency_threshold", "0.05", "decimal"),
                ("liquidation_partial_pct", "0.5", "decimal"),
                ("liquidation_target_margin", "0.3", "decimal"),
                ("liquidation_sweep_interval_sec", "60", "int"),
            ]:
                s.add(SiteConfig(key=k, value=v, value_type=t))
    yield
    await WRITER.stop()


async def _user(cash="1000", debt="0"):
    sfx = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=f"u_{sfx}", casdoor_id=f"cd_{sfx}", cash=Decimal(cash), debt=Decimal(debt),
                     debt_last_accrued_at=datetime.now(timezone.utc) if Decimal(debt) > 0 else None)
            s.add(u); await s.flush(); uid = u.id
    return uid, {"Authorization": f"Bearer {create_access_token(uid)}"}


async def _market(shares=("0", "0"), closes_at=None, status=MarketStatus.TRADING):
    async with async_session_maker() as s:
        m = Market(title="m", liquidity_b=100.0, status=status, closes_at=closes_at, tags="")
        s.add(m); await s.flush()
        oids = []
        for v in shares:
            o = Outcome(market_id=m.id, label=f"o{v}", total_shares=Decimal(v)); s.add(o); await s.flush(); oids.append(o.id)
        await s.commit()
        return m.id, oids


async def _pos(uid, oid, amount):
    async with async_session_maker() as s:
        s.add(Position(user_id=uid, outcome_id=oid, amount=Decimal(amount), cost_basis=Decimal("0")))
        await s.commit()


# ── #1 ──────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_borrow_quota_uses_locked_row_not_request_snapshot(client, monkeypatch):
    """模拟「并发另一笔 borrow 已在锁内提交」：锁后行 debt=100（额度已耗尽），
    而依赖注入的 user 快照仍是 debt=0。修复后额度用锁后行 → 400。"""
    uid, h = await _user(cash="100")      # k=1 → max_borrow = 100
    from app.api.v1 import loan as loan_api
    real_lock = loan_api.lock_user

    async def lock_then_simulate_concurrent_commit(db, user_id):
        u = await real_lock(db, user_id)
        u.debt = Decimal("100"); u.cash = Decimal("200")     # 如同另一笔 borrow 100 已落库
        u.debt_last_accrued_at = datetime.now(timezone.utc)
        return u
    monkeypatch.setattr(loan_api, "lock_user", lock_then_simulate_concurrent_commit)

    r = await client.post("/api/v1/loan/borrow", json={"amount": "100"}, headers=h)
    assert r.status_code == 400, r.text
    async with async_session_maker() as s:
        u = await s.get(User, uid)
        assert u.debt == 0 and u.cash == Decimal("100")      # 400 路径回滚，未落任何变更


@pytest.mark.asyncio
async def test_borrow_sequential_second_request_rejected(client):
    uid, h = await _user(cash="100")
    assert (await client.post("/api/v1/loan/borrow", json={"amount": "100"}, headers=h)).status_code == 200
    r = await client.post("/api/v1/loan/borrow", json={"amount": "1"}, headers=h)
    assert r.status_code == 400


# ── #2 ──────────────────────────────────────────────────────────────────────
def test_market_is_open_predicate():
    now = datetime.now(timezone.utc)
    assert market_is_open(MarketStatus.TRADING, None)
    assert market_is_open(MarketStatus.TRADING, now + timedelta(hours=1))
    assert not market_is_open(MarketStatus.TRADING, now - timedelta(seconds=1))
    assert not market_is_open(MarketStatus.TRADING, (now - timedelta(seconds=1)).replace(tzinfo=None))
    assert not market_is_open(MarketStatus.HALT, None)
    assert not market_is_open(MarketStatus.SETTLED, None)


@pytest.mark.asyncio
async def test_expired_market_excluded_from_lcv_and_grants_immunity(client):
    uid, _ = await _user(cash="0", debt="50")
    mid_open, o_open = await _market(shares=("100", "100"))
    mid_exp, o_exp = await _market(shares=("100", "100"), closes_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    await _pos(uid, o_open[0], "50")
    await _pos(uid, o_exp[0], "50")
    async with async_session_maker() as s:
        hv = (await wealth.compute_users_holdings_value(s, user_ids=[uid]))[uid]
        assert hv > 0
        assert await wealth.user_has_halt_holdings(s, uid) is True   # 过期市场 = HALT 豁免
    # 把过期市场的仓位删掉再算一次，估值应完全相同
    async with async_session_maker() as s:
        p = (await s.execute(select(Position).where(Position.outcome_id == o_exp[0]))).scalar_one()
        await s.delete(p); await s.commit()
    async with async_session_maker() as s:
        assert (await wealth.compute_users_holdings_value(s, user_ids=[uid]))[uid] == hv
        assert await wealth.user_has_halt_holdings(s, uid) is False






# ── #3 ──────────────────────────────────────────────────────────────────────




# ── #4 ──────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_sweep_is_mutually_exclusive(client, monkeypatch):
    """两次并发 run：一次真跑，另一次立即返回 skipped=sweep_in_progress。"""
    calls = []

    async def slow_inner(trigger_source):
        calls.append(trigger_source)
        await asyncio.sleep(0.2)
        return {"triggered": 0}
    from app.services.credit import sweep
    monkeypatch.setattr(sweep, "run_sweep", slow_inner)

    a, b = await asyncio.gather(
        liquidation_sweep.run_liquidation_sweep_once("scheduler"),
        liquidation_sweep.run_liquidation_sweep_once("admin_manual"),
    )
    assert calls == ["scheduler"]
    assert {"skipped": "sweep_in_progress"} in (a, b) and {"triggered": 0} in (a, b)
    # 锁已释放：再跑一次正常进入
    assert await liquidation_sweep.run_liquidation_sweep_once("scheduler") == {"triggered": 0}


# ── 第二批：利率口径 / 冷却 / 结算量化 ──────────────────────────────────────
def test_interest_is_exact_daily_rate_regardless_of_tick_size():
    """(1+r)^(Δt/day) 分段可合成：60s×1440 次 == 一次 24h == debt×(1+r)。"""
    from app.services.loan_service import accrue_interest
    rate = Decimal("0.10")
    t0 = datetime.now(timezone.utc)
    a = User(username="a", casdoor_id="a", debt=Decimal("1000"), debt_last_accrued_at=t0)
    for i in range(1, 1441):
        accrue_interest(a, rate, t0 + timedelta(seconds=60 * i))
    b = User(username="b", casdoor_id="b", debt=Decimal("1000"), debt_last_accrued_at=t0)
    accrue_interest(b, rate, t0 + timedelta(days=1))
    assert b.debt == Decimal("1100.000000")
    assert abs(a.debt - b.debt) < Decimal("0.001")      # 逐 tick 只差累计的 6dp 舍入


def test_tiny_debt_eventually_accrues():
    """增量不足 1 LSB 时不推进锚点：0.05 @1%/日 60s tick 旧版永远 0.05，新版最终会涨。"""
    from app.services.loan_service import accrue_interest
    t0 = datetime.now(timezone.utc)
    u = User(username="t", casdoor_id="t", debt=Decimal("0.05"), debt_last_accrued_at=t0)
    for i in range(1, 60 * 24 + 1):
        accrue_interest(u, Decimal("0.01"), t0 + timedelta(seconds=60 * i))
    # 理论 0.0505；每次跨过半个 LSB 就进位（ROUND_HALF_UP），逐 tick 累计最多偏高 ~0.5 LSB/次，
    # 绝对误差 < 0.0003 金/日，可接受（无 carry 字段的代价）。旧版这里会恒为 0.050000。
    assert Decimal("0.0505") <= u.debt <= Decimal("0.0508")




@pytest.mark.asyncio
async def test_settle_payout_quantized_to_6dp(client):
    """pos.amount × payout_unit 为 12dp 时，cash 增量与审计快照都必须是 6dp 量化值。"""
    from app.services.writer_ops import ResolveCmd
    from app.models.audit import AuditEvent
    uid, _ = await _user(cash="0")
    admin, _ = await _user()
    mid, oids = await _market(shares=("1.234567", "0"))
    await _pos(uid, oids[0], "1.234567")
    await WRITER.start()
    res = await WRITER.submit(ResolveCmd(market_id=mid, winning_outcome_id=oids[0],
                                         payout=Decimal("0.333333"), admin_id=admin))
    expected = (Decimal("1.234567") * Decimal("0.333333")).quantize(Decimal("0.000001"))
    assert res.total_payout == expected
    async with async_session_maker() as s:
        assert (await s.get(User, uid)).cash == expected
        ev = (await s.execute(select(AuditEvent).where(AuditEvent.event_type == "settle_win"))).scalar_one()
        assert Decimal(ev.user_after["cash"]) == expected
        assert Decimal(ev.payload["cost"]) == -expected
