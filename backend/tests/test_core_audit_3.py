"""核心审计 3（2026-08-22）修复回归：
#1 请求 session 内「先 db.get(User) 再 SELECT FOR UPDATE」必须拿到锁后的最新行，
   而不是 identity map 里的陈旧对象（SQLAlchemy 默认不刷新已加载实体）。
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.base import SiteConfig, User
from app.services import loan_service
from app.services.market_locks import lock_user


@pytest_asyncio.fixture(autouse=True)
async def _cfg(setup_db):
    async with async_session_maker() as s:
        async with s.begin():
            for k, v, t in [
                ("loan_enabled", "true", "bool"), ("loan_leverage_k", "1.0", "decimal"),
                ("loan_daily_rate", "0.01", "decimal"),
            ]:
                s.add(SiteConfig(key=k, value=v, value_type=t))
    yield


async def _user(cash="100", debt="0"):
    sfx = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=f"u_{sfx}", casdoor_id=f"cd_{sfx}", cash=Decimal(cash), debt=Decimal(debt),
                     debt_last_accrued_at=datetime.now(timezone.utc) if Decimal(debt) > 0 else None)
            s.add(u); await s.flush(); return u.id


async def _concurrent_commit(uid, cash):
    """模拟另一条连接（writer 交易）已提交的 cash 变动。"""
    async with async_session_maker() as other:
        async with other.begin():
            u = (await other.execute(select(User).where(User.id == uid))).scalar_one()
            u.cash = Decimal(cash)


@pytest.mark.asyncio
async def test_lock_user_refreshes_stale_identity_map():
    uid = await _user(cash="100")
    async with async_session_maker() as db:
        stale = await db.get(User, uid)                 # 如同 get_current_user
        assert stale.cash == Decimal("100")
        await _concurrent_commit(uid, "70")
        locked = await lock_user(db, uid)
        assert locked is stale                           # 同一对象
        assert locked.cash == Decimal("70")              # 但属性已刷新


@pytest.mark.asyncio
async def test_increase_and_decrease_debt_use_fresh_row():
    uid = await _user(cash="100")
    async with async_session_maker() as db:
        await db.get(User, uid)
        await _concurrent_commit(uid, "70")
        u = await loan_service.increase_debt(
            db, uid, Decimal("10"), daily_rate=Decimal("0.01"), source="borrow", grant_cash=True,
        )
        assert u.cash == Decimal("80")                   # 70 + 10，而不是 100 + 10
        await db.commit()

    async with async_session_maker() as db:
        await db.get(User, uid)
        await _concurrent_commit(uid, "50")
        u, eff = await loan_service.decrease_debt(
            db, uid, Decimal("10"), daily_rate=Decimal("0.01"), source="repay", consume_cash=True,
        )
        assert eff == Decimal("10")
        assert u.cash == Decimal("40")                   # 50 − 10
        await db.commit()

    async with async_session_maker() as s:
        u = await s.get(User, uid)
        assert u.cash == Decimal("40") and u.debt == Decimal("0")


# ── #2 writer 路径提交前释放请求 session 的池连接 ───────────────────────────
@pytest.mark.asyncio
async def test_writer_buy_releases_request_connection_before_submit(client, monkeypatch):
    from sqlalchemy import text
    from app.services.site_config import clear_cache
    from app.services.market_writer import WRITER
    from app.api.v1 import market as market_api
    from tests.test_writer_e2e import _dev_login, _create_market

    async with async_session_maker() as s:
        await s.execute(text(
            "INSERT INTO siteconfig (key, value, value_type, updated_at) "
            "VALUES ('single_writer_enabled', 'true', 'bool', CURRENT_TIMESTAMP) "
            "ON CONFLICT (key) DO UPDATE SET value='true'"))
        await s.commit()
    clear_cache()
    await WRITER.start()
    try:
        admin_h = await _dev_login(client, "admin_ca3")
        mid, oids = await _create_market(client, admin_h, "ca3")
        user_h = await _dev_login(client, "user_ca3")

        seen = {}
        real_submit = WRITER.submit
        real_release = market_api._release_request_connection

        async def spy_release(db):
            await real_release(db)
            seen["db"] = db

        async def spy_submit(cmd):
            db = seen.get("db")
            assert db is not None, "submit 前必须先释放请求连接"
            assert not db.in_transaction(), "请求 session 仍持有事务/连接"
            return await real_submit(cmd)

        monkeypatch.setattr(market_api, "_release_request_connection", spy_release)
        monkeypatch.setattr(WRITER, "submit", spy_submit)

        r = await client.post("/api/v1/market/buy", headers=user_h,
                              json={"outcome_id": oids[0], "shares": "1"})
        assert r.status_code == 200, r.text
        r = await client.post("/api/v1/market/sell", headers=user_h,
                              json={"outcome_id": oids[0], "shares": "1"})
        assert r.status_code == 200, r.text
    finally:
        await WRITER.stop()


# ── M3 强平：债清即停，不再卖剩余市场 ─────────────────────────────────────
@pytest.mark.asyncio
async def test_split_liquidation_stops_after_debt_cleared(client):
    from app.models.base import Market, MarketStatus, Outcome, Position, LiquidationEvent
    from app.services import liquidation_service
    from app.services.market_writer import WRITER
    from sqlalchemy import text
    async with async_session_maker() as s:
        async with s.begin():
            for k, v, t in [("sell_fee_rate", "0", "decimal"), ("liquidation_enabled", "true", "bool")]:
                s.add(SiteConfig(key=k, value=v, value_type=t))

    async def market(shares):
        async with async_session_maker() as s:
            m = Market(title="m", liquidity_b=100.0, status=MarketStatus.TRADING, tags="")
            s.add(m); await s.flush()
            oids = []
            for v in shares:
                o = Outcome(market_id=m.id, label=f"o{v}", total_shares=Decimal(v)); s.add(o); await s.flush(); oids.append(o.id)
            await s.commit(); return m.id, oids

    # m1：200 股在 q=[300,100] 清算价 ≈ 143；m2：10 股在 q=[200,100] ≈ 7。债 140：
    # margin ≈ (150−140)/140 = 0.07 < hard 0.2 触发；m1 一卖即清债，m2 不该动
    uid = await _user(cash="0", debt="140")
    m1, o1 = await market(("300", "100"))
    m2, o2 = await market(("200", "100"))
    async with async_session_maker() as s:
        s.add(Position(user_id=uid, outcome_id=o1[0], amount=Decimal("200"), cost_basis=Decimal("0")))
        s.add(Position(user_id=uid, outcome_id=o2[0], amount=Decimal("10"), cost_basis=Decimal("0")))
        await s.commit()
    await WRITER.start()
    try:
        ev = await liquidation_service.liquidate_user_split(
            uid, daily_rate=Decimal("0.01"), trigger_source="scheduler",
            partial_pct=Decimal("1"), target_margin=Decimal("0.3"),
            emergency_threshold=Decimal("0.05"), hard_threshold=Decimal("0.2"))
        assert isinstance(ev, LiquidationEvent)
        assert ev.sold_positions_count == 1, "债清后第二个市场不该再卖"
        assert ev.remaining_debt == 0
        async with async_session_maker() as s:
            remaining = (await s.execute(
                select(Position.amount).where(Position.user_id == uid, Position.outcome_id == o2[0]))).scalar_one()
            assert remaining == Decimal("10")
    finally:
        await WRITER.stop()


# ── P1 24h 前价格：关联子查询取每个 outcome cutoff 前最后一笔 ───────────────
@pytest.mark.asyncio
async def test_last_price_before_picks_latest_before_cutoff():
    from datetime import timedelta
    from app.models.base import Market, MarketStatus, Outcome, Transaction
    from app.api.v1.market import _last_price_before, _get_prices_24h_ago
    uid = await _user()
    async with async_session_maker() as s:
        m = Market(title="m", liquidity_b=100.0, status=MarketStatus.TRADING, tags="")
        s.add(m); await s.flush()
        o1 = Outcome(market_id=m.id, label="a", total_shares=Decimal("0"))
        o2 = Outcome(market_id=m.id, label="b", total_shares=Decimal("0"))
        s.add(o1); s.add(o2); await s.flush()
        now = datetime.now(timezone.utc)
        def tx(oid, hours_ago, price):
            return Transaction(user_id=uid, outcome_id=oid, type="buy", shares=Decimal("1"),
                               price=Decimal(price), cost=Decimal("1"),
                               timestamp=now - timedelta(hours=hours_ago))
        s.add(tx(o1.id, 30, "0.30")); s.add(tx(o1.id, 25, "0.25")); s.add(tx(o1.id, 1, "0.90"))
        s.add(tx(o2.id, 2, "0.50"))          # o2 在 24h 内才有成交
        await s.commit()
        ids = [o1.id, o2.id]

    async with async_session_maker() as s:
        got = await _get_prices_24h_ago(s, ids)
        assert got == {o1.id: 0.25}
        cutoff = datetime.now(timezone.utc) - timedelta(hours=26)
        assert await _last_price_before(s, ids, cutoff, Transaction.price) == {o1.id: 0.30}
        assert await _last_price_before(s, [], cutoff, Transaction.price) == {}


# ── P3/L8 quote：writer 开启时用内存 state；与成交同源；过 closes_at 拒绝 ──────
@pytest.mark.asyncio
async def test_quote_matches_fill_and_respects_closes_at(client):
    from datetime import timedelta
    from sqlalchemy import text
    from app.services.site_config import clear_cache
    from app.services.market_writer import WRITER
    from app.api.v1 import market as market_api
    from app.models.base import Market
    from tests.test_writer_e2e import _dev_login, _create_market

    async with async_session_maker() as s:
        await s.execute(text(
            "INSERT INTO siteconfig (key, value, value_type, updated_at) "
            "VALUES ('single_writer_enabled', 'true', 'bool', CURRENT_TIMESTAMP) "
            "ON CONFLICT (key) DO UPDATE SET value='true'"))
        await s.commit()
    clear_cache()
    await WRITER.start()
    try:
        admin_h = await _dev_login(client, "admin_q")
        mid, oids = await _create_market(client, admin_h, "q")
        user_h = await _dev_login(client, "user_q")

        q = await client.post("/api/v1/market/quote", headers=user_h,
                              json={"outcome_id": oids[0], "shares": "7.1234567", "side": "buy"})
        assert q.status_code == 200, q.text
        b = await client.post("/api/v1/market/buy", headers=user_h,
                              json={"outcome_id": oids[0], "shares": "7.1234567", "accept_any_slippage": True})
        assert b.status_code == 200, b.text
        assert q.json()["net"] == b.json()["pay"]          # 报价与成交同源（含 6dp 量化）

        # 第二次报价必须反映 writer 内存里的新 q（不同 shares 绕过 1s 缓存）
        q2 = await client.post("/api/v1/market/quote", headers=user_h,
                               json={"outcome_id": oids[0], "shares": "7.2", "side": "buy"})
        assert q2.json()["net"] > q.json()["net"]

        # 过 closes_at：writer 内存态 + DB 路径都应 400
        async with async_session_maker() as s:
            m = await s.get(Market, mid)
            m.closes_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            await s.commit()
        await WRITER.reload_state(mid)
        market_api._QUOTE_CACHE.clear()
        r = await client.post("/api/v1/market/quote", headers=user_h,
                              json={"outcome_id": oids[0], "shares": "1", "side": "buy"})
        assert r.status_code == 400
        await WRITER.stop()
        market_api._QUOTE_CACHE.clear()
        r = await client.post("/api/v1/market/quote", headers=user_h,
                              json={"outcome_id": oids[0], "shares": "1", "side": "buy"})
        assert r.status_code == 400
    finally:
        await WRITER.stop()


# ── M4 snapshot 携带尾巴已覆盖到的最后成交 id ─────────────────────────────
@pytest.mark.asyncio
async def test_snapshot_reports_history_tail_through_trade_id(client):
    from sqlalchemy import text
    from app.services.site_config import clear_cache
    from app.services.market_writer import WRITER
    from app.api.v1.stream import _build_snapshot
    from tests.test_writer_e2e import _dev_login, _create_market

    async with async_session_maker() as s:
        await s.execute(text(
            "INSERT INTO siteconfig (key, value, value_type, updated_at) "
            "VALUES ('single_writer_enabled', 'true', 'bool', CURRENT_TIMESTAMP) "
            "ON CONFLICT (key) DO UPDATE SET value='true'"))
        await s.commit()
    clear_cache()
    await WRITER.start()
    try:
        admin_h = await _dev_login(client, "admin_m4")
        mid, oids = await _create_market(client, admin_h, "m4")
        user_h = await _dev_login(client, "user_m4")
        async with async_session_maker() as s:
            assert (await _build_snapshot(s, mid))["history_tail_through_trade_id"] == 0
        for _ in range(2):
            r = await client.post("/api/v1/market/buy", headers=user_h,
                                  json={"outcome_id": oids[0], "shares": "1", "accept_any_slippage": True})
            assert r.status_code == 200, r.text
        from app.models.base import Transaction
        from sqlalchemy import func
        async with async_session_maker() as s:
            last_id = (await s.execute(select(func.max(Transaction.id)))).scalar_one()
            snap = await _build_snapshot(s, mid)
        assert snap["history_tail_through_trade_id"] == last_id
        assert sum(snap["history_tail"][str(oids[0])]["10s"]["v"]) == 2.0
    finally:
        await WRITER.stop()
