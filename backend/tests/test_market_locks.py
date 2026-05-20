"""market_locks.py 单元测：基础锁正确性 + 不存在抛 404。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
from fastapi import HTTPException

from app.models.base import Market, Outcome, User, MarketStatus
from app.services.market_locks import (
    lock_market, lock_outcome, lock_outcomes_for_market, lock_user,
)


@pytest.mark.asyncio
async def test_lock_user_not_exists_raises_404(client):
    from app.core.database import async_session_maker
    async with async_session_maker() as db:
        with pytest.raises(HTTPException) as exc:
            await lock_user(db, 99999)
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_lock_market_not_exists_raises_404(client):
    from app.core.database import async_session_maker
    async with async_session_maker() as db:
        with pytest.raises(HTTPException) as exc:
            await lock_market(db, 99999)
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_lock_outcomes_returns_id_asc(client):
    from app.core.database import async_session_maker
    async with async_session_maker() as db:
        m = Market(title="t", description="t", liquidity_b=1000,
                   status=MarketStatus.TRADING, tags="")
        db.add(m); await db.flush()
        o1 = Outcome(market_id=m.id, label="A", total_shares=0)
        o2 = Outcome(market_id=m.id, label="B", total_shares=0)
        db.add(o1); db.add(o2)
        await db.commit()

        result = await lock_outcomes_for_market(db, m.id)
        assert [o.id for o in result] == sorted([o.id for o in result])
