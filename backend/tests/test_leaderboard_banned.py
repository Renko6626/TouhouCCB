"""封禁用户（is_active=false）不应出现在财富 / 消费排行榜。"""
import uuid
from decimal import Decimal

import pytest

from app.api.v1.market import clear_leaderboard_cache
from app.core.database import async_session_maker
from app.models.base import User
from app.models.redemption import DanmukuExchange


async def _seed(prefix: str, cash: str, *, active: bool = True) -> int:
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(
                username=f"{prefix}_{suffix}",
                casdoor_id=f"cd_{suffix}",
                cash=Decimal(cash),
                is_active=active,
            )
            s.add(u)
            await s.flush()
            return u.id


@pytest.mark.asyncio
async def test_net_worth_leaderboard_excludes_banned(client):
    banned = await _seed("banned", "99999", active=False)
    active = await _seed("active", "100")
    clear_leaderboard_cache()
    r = await client.get("/api/v1/market/leaderboard?mode=net_worth&limit=100")
    assert r.status_code == 200, r.text
    ids = [it["user_id"] for it in r.json()]
    assert banned not in ids
    assert active in ids


@pytest.mark.asyncio
async def test_spending_leaderboard_excludes_banned(client):
    banned = await _seed("banned", "100", active=False)
    active = await _seed("active", "100")
    async with async_session_maker() as s:
        async with s.begin():
            for uid, amount in ((banned, "50"), (active, "10")):
                s.add(DanmukuExchange(
                    user_id=uid, qq_user_id="1", room_id="r",
                    yuan=Decimal(amount), huo=Decimal("0"),
                    amount=Decimal(amount), code_string="code",
                ))
    clear_leaderboard_cache()
    r = await client.get("/api/v1/market/leaderboard?mode=spending&limit=100")
    assert r.status_code == 200, r.text
    ids = [it["user_id"] for it in r.json()]
    assert banned not in ids
    assert active in ids
