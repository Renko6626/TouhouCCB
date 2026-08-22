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
