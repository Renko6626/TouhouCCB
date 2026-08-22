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
