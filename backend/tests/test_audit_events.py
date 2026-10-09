"""审计事件流：每条写路径落事件 + 折叠/增量校验与线上表一致。

场景走 HTTP API（legacy 与 writer 两条路径各跑一遍），覆盖：
注册(dev-login) → 建市场 → 买/卖 → 借/还 → 定时结息 → 熔断/恢复 → 结算 → 管理员调账/大赦 → 配置变更。
强平单独用 service 层跑（legacy）与 split 路径跑（writer）。
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.audit import AuditEvent
from app.models.base import Market, Outcome, Position, SiteConfig, User
from app.services import audit_replay, audit_service
from app.services.loan_sweep import run_sweep_once
from app.services.market_locks import lock_user
from app.services.market_writer import WRITER


@pytest_asyncio.fixture(autouse=True)
async def _cfg(setup_db):
    async with async_session_maker() as s:
        async with s.begin():
            for k, v, t in [
                ("loan_enabled", "true", "bool"),
                ("loan_leverage_k", "1.0", "decimal"),
                ("loan_daily_rate", "0.10", "decimal"),
                ("loan_sweep_interval_sec", "60", "int"),
                ("initial_balance", "1000", "decimal"),
                ("sell_fee_rate", "0.02", "decimal"),
                ("liquidation_hard_threshold", "0.2", "decimal"),
                ("liquidation_soft_threshold", "0.5", "decimal"),
                ("liquidation_emergency_threshold", "0.05", "decimal"),
                ("liquidation_partial_pct", "0.5", "decimal"),
                ("liquidation_target_margin", "0.3", "decimal"),
            ]:
                s.add(SiteConfig(key=k, value=v, value_type=t))
    yield
    await WRITER.stop()


async def _events(**where) -> list[AuditEvent]:
    async with async_session_maker() as s:
        stmt = select(AuditEvent).order_by(AuditEvent.id.asc())
        for k, v in where.items():
            stmt = stmt.where(getattr(AuditEvent, k) == v)
        return list((await s.execute(stmt)).scalars().all())


async def _dev_login(client, username: str) -> tuple[int, dict]:
    r = await client.post("/api/v1/auth/dev-login", json={"username": username})
    assert r.status_code == 200, r.text
    body = r.json()
    uid = body["user"]["id"] if "user" in body else body.get("user_id")
    if uid is None:
        async with async_session_maker() as s:
            uid = (await s.execute(select(User.id).where(User.username == username))).scalar_one()
    return uid, {"Authorization": f"Bearer {body['access_token']}"}


async def _fold_and_verify(check=True):
    async with async_session_maker() as s:
        evs = await audit_replay.load_events(s)
        snap, mism = audit_replay.fold(evs, check=check)
        live = await audit_replay.compare_with_live(s, snap)
    return evs, snap, mism, live


async def _scenario(client, *, writer: bool):
    """完整一轮。返回 (admin_uid, alice_uid, market_id, outcome_ids)。"""
    if writer:
        await WRITER.start()
    admin_uid, ah = await _dev_login(client, "admin_a")      # 首用户 = 超管
    alice_uid, h = await _dev_login(client, "alice_a")

    r = await client.post("/api/v1/market/create", headers=ah, json={
        "title": "m", "description": "", "liquidity_b": 100, "outcomes": ["A", "B"], "tags": [],
    })
    assert r.status_code == 201, r.text
    mid = r.json()["market_id"]
    async with async_session_maker() as s:
        oids = list((await s.execute(
            select(Outcome.id).where(Outcome.market_id == mid).order_by(Outcome.id))).scalars().all())

    # 买 / 卖（卖收 2% 手续费）
    for oid, sh in [(oids[0], "10"), (oids[1], "4")]:
        r = await client.post("/api/v1/market/buy", headers=h,
                              json={"outcome_id": oid, "shares": sh, "accept_any_slippage": True})
        assert r.status_code == 200, r.text
    r = await client.post("/api/v1/market/sell", headers=h,
                          json={"outcome_id": oids[0], "shares": "3", "accept_any_slippage": True})
    assert r.status_code == 200, r.text

    # 借 / 还
    # Establish the old accrual clock through the real borrow writer so its
    # audit anchor agrees with the debt row; retroactively editing only the DB
    # clock would correctly be detected as unaudited history by replay.
    prior = datetime.now(timezone.utc) - timedelta(days=1)
    with patch("app.api.v1.loan.datetime", wraps=datetime) as clock:
        clock.now.return_value = prior
        r = await client.post("/api/v1/loan/borrow", json={"amount": "100"}, headers=h)
    assert r.status_code == 200, r.text
    assert await run_sweep_once() == 1
    r = await client.post("/api/v1/loan/repay", json={"amount": "30"}, headers=h)
    assert r.status_code == 200, r.text

    # 熔断 / 恢复
    assert (await client.post(f"/api/v1/market/{mid}/close", headers=ah)).status_code == 200
    assert (await client.post(f"/api/v1/market/{mid}/resume", headers=ah)).status_code == 200

    # 管理员调账 + 配置变更
    r = await client.post(f"/api/v1/admin/users/{alice_uid}/cash", headers=ah,
                          json={"amount": "50", "reason": "t"})
    assert r.status_code == 200, r.text
    r = await client.put("/api/v1/admin/site-config/liquidation_partial_pct", headers=ah, json={"value": "0.25"})
    assert r.status_code == 200, r.text

    # 结算：A 赢，payout 1
    r = await client.post(f"/api/v1/market/{mid}/resolve", headers=ah,
                          json={"winning_outcome_id": oids[0], "payout": "1"})
    assert r.status_code == 200, r.text

    # 大赦
    r = await client.post("/api/v1/admin/users/batch/amnesty", headers=ah,
                          json={"filter": {}, "reason": "t", "dry_run": False})
    assert r.status_code == 200, r.text
    return admin_uid, alice_uid, mid, oids


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", [False, True])
async def test_full_scenario_emits_events_and_replays_consistently(client, writer):
    admin_uid, alice_uid, mid, oids = await _scenario(client, writer=writer)

    types = [e.event_type for e in await _events()]
    for t in ["user_register", "market_create", "trade_buy", "trade_sell", "loan_borrow",
              "interest_accrual", "loan_repay", "market_close", "market_resume",
              "admin_adjust_cash", "config_set", "settle_win", "settle_lose",
              "market_settle", "admin_amnesty"]:
        assert t in types, f"missing {t}: {types}"
    path = "writer" if writer else "legacy"
    buys = await _events(event_type="trade_buy")
    assert all(b.payload["path"] == path for b in buys)

    # 交易事件带全市场 q 向量、持仓快照、用户快照、ref 到 transaction
    b0 = buys[0]
    assert b0.market_after["outcome_ids"] == oids
    assert b0.market_after["q"] == ["10.000000", "0.000000"]
    assert len(b0.market_after["prices"]) == 2 and b0.market_after["b"] == 100.0
    assert b0.position_after["amount"] == "10.000000"
    assert Decimal(b0.user_after["cash"]) == Decimal("1000") - Decimal(b0.payload["cost"])
    assert b0.ref_table == "transaction" and b0.ref_id is not None
    sell = (await _events(event_type="trade_sell"))[0]
    assert sell.payload["fee_rate"] == "0.02" and Decimal(sell.payload["fee"]) > 0

    # 结息事件：带利息/利率/间隔
    acc = (await _events(event_type="interest_accrual"))[0]
    assert acc.user_id == alice_uid and Decimal(acc.payload["interest"]) > 0
    assert acc.payload["daily_rate"] == "0.10" and acc.payload["elapsed_sec"] > 86000
    # 还款事件携带隐式结息（sweep 后到 repay 间隔极短，但应 ≥ 0 且与快照自洽）
    rep = (await _events(event_type="loan_repay"))[0]
    assert Decimal(rep.payload["interest_accrued"]) >= 0

    cfg = (await _events(event_type="config_set"))[0]
    assert cfg.payload == {"key": "liquidation_partial_pct", "old": "0.5", "new": "0.25", "value_type": "decimal"}
    assert cfg.operator_user_id == admin_uid

    settle = (await _events(event_type="market_settle"))[0]
    assert settle.payload["winning_outcome_id"] == oids[0]
    assert settle.market_after["status"] == "settled"
    lose = (await _events(event_type="settle_lose"))[0]
    assert lose.position_after["amount"] == "0" and lose.outcome_id == oids[1]

    amn = (await _events(event_type="admin_amnesty"))
    assert {e.user_id for e in amn} == {alice_uid}       # 超管默认排除

    # ── 折叠 + 独立增量校验 + 线上表比对：全部为空 ──
    evs, snap, mism, live = await _fold_and_verify()
    assert mism == [], mism
    assert live == [], live
    assert snap.users[alice_uid].anchored and snap.markets[mid].anchored
    assert snap.users[alice_uid].cash == Decimal("1000") and snap.users[alice_uid].debt == 0
    assert all(v == 0 for v in snap.positions.values())

    # ── 时间点查询：截到第一笔买入 → alice 持仓 10 / 市场 q=[10,0] ──
    upto = buys[0].id
    snap_t, _ = audit_replay.fold([e for e in evs if e.id <= upto])
    assert snap_t.positions[(alice_uid, oids[0])] == Decimal("10")
    assert snap_t.markets[mid].q == [Decimal("10"), Decimal("0")]
    assert snap_t.users[alice_uid].cash == Decimal(b0.user_after["cash"])






@pytest.mark.asyncio
async def test_ban_role_and_register_events(client):
    admin_uid, ah = await _dev_login(client, "admin_r")
    uid, _ = await _dev_login(client, "carol_r")
    regs = await _events(event_type="user_register")
    assert [e.user_id for e in regs] == [admin_uid, uid]
    assert regs[0].payload["is_superuser"] is True and regs[1].payload["is_superuser"] is False
    assert Decimal(regs[1].user_after["cash"]) == Decimal("1000")

    assert (await client.patch(f"/api/v1/admin/users/{uid}/ban", headers=ah, json={"reason": "t"})).status_code == 200
    assert (await client.patch(f"/api/v1/admin/users/{uid}/unban", headers=ah)).status_code == 200
    assert (await client.patch(f"/api/v1/admin/users/{uid}/role", headers=ah,
                               json={"is_admin": True})).status_code == 200
    tail = [(e.event_type, e.user_id, e.operator_user_id) for e in (await _events())[-3:]]
    assert tail == [("admin_ban", uid, admin_uid), ("admin_unban", uid, admin_uid),
                    ("admin_set_role", uid, admin_uid)]


@pytest.mark.asyncio
async def test_fold_detects_tampered_snapshot(client):
    """折叠器确实在校验：人为改坏一条事件的 user_after → 报 mismatch。"""
    _, ah = await _dev_login(client, "admin_t")
    uid, h = await _dev_login(client, "dan_t")
    r = await client.post("/api/v1/loan/borrow", json={"amount": "100"}, headers=h)
    assert r.status_code == 200, r.text
    async with async_session_maker() as s:
        async with s.begin():
            ev = (await s.execute(select(AuditEvent).where(AuditEvent.event_type == "loan_borrow"))).scalar_one()
            ev.user_after = {**ev.user_after, "cash": "999999"}
    _, snap, mism, live = await _fold_and_verify()
    assert any(m.field == "cash" and m.entity == f"user:{uid}" for m in mism)
    assert any(m.entity == f"user:{uid}" for m in live)


@pytest.mark.asyncio
async def test_repay_noop_still_records_interest_accrual(client):
    """还款量化为 0（cash=0）但结息已改写 debt → 必须有 interest_accrual 事件，折叠仍自洽。"""
    from app.services import loan_service
    async with async_session_maker() as s:
        u = User(username="noop", casdoor_id="c_noop", cash=Decimal("0"), debt=Decimal("100"),
                 debt_last_accrued_at=datetime.now(timezone.utc) - timedelta(days=1))
        s.add(u); await s.flush()
        audit_service.record(s, "user_register", user_id=u.id, user_after=audit_service.user_snapshot(u))
        await s.commit(); uid = u.id
    async with async_session_maker() as s:
        async with s.begin():
            _, eff = await loan_service.decrease_debt(
                s, uid, Decimal("10"), consume_cash=True, daily_rate=Decimal("0.10"), source="repay")
    assert eff == 0
    evs = await _events(user_id=uid)
    assert [e.event_type for e in evs] == ["user_register", "interest_accrual"]
    assert evs[-1].payload["source"] == "repay_noop" and Decimal(evs[-1].payload["interest"]) > 0
    _, snap, mism, live = await _fold_and_verify()
    assert mism == [] and live == []
    assert snap.users[uid].debt > Decimal("100")
