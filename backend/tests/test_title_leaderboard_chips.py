"""Task 14: leaderboard / recent_liquidations 响应里附带 equipped_title chip。

覆盖：
- /market/leaderboard (net_worth mode) 装备了 title 的用户行带 equipped_title chip
- /market/leaderboard 未装备时为 null
- /loan/recent-liquidations 强平事件主用户的 equipped_title 一起返回
"""
import pytest
import uuid
from decimal import Decimal
from datetime import datetime, timezone

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import User, LiquidationEvent
from app.models.title import Title, UserTitle


async def _mk_user_with_equipped_title(name_prefix: str = "top"):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(
                username=f"{name_prefix}_{suffix}",
                email=f"t{suffix}@x.com",
                casdoor_id=f"cd_{suffix}",
                cash=Decimal("99999"),
            )
            t = Title(name=f"VIP_{suffix}", color="#FFD700", icon="★")
            s.add(u)
            s.add(t)
            await s.flush()
            s.add(UserTitle(user_id=u.id, title_id=t.id, source="admin"))
            u.equipped_title_id = t.id
            s.add(u)
            await s.flush()
            return u.id, t.id, suffix


@pytest.mark.asyncio
async def test_leaderboard_includes_equipped_title(client):
    uid, tid, sfx = await _mk_user_with_equipped_title("toplead")
    h = {"Authorization": f"Bearer {create_access_token(uid)}"}
    r = await client.get("/api/v1/market/leaderboard", headers=h)
    assert r.status_code == 200, r.text
    items = r.json()
    mine = next(
        (it for it in items if it.get("username", "").startswith("toplead_")),
        None,
    )
    assert mine is not None, f"new user not in leaderboard: {items}"
    assert mine.get("equipped_title") is not None
    assert mine["equipped_title"]["color"] == "#FFD700"
    assert mine["equipped_title"]["icon"] == "★"
    assert mine["equipped_title"]["id"] == tid


@pytest.mark.asyncio
async def test_leaderboard_no_equipped_returns_null(client):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(
                username=f"nochip_{suffix}",
                email=f"nc{suffix}@x.com",
                casdoor_id=f"nc_{suffix}",
                cash=Decimal("50000"),
            )
            s.add(u)
            await s.flush()
            uid = u.id
    h = {"Authorization": f"Bearer {create_access_token(uid)}"}
    r = await client.get("/api/v1/market/leaderboard", headers=h)
    assert r.status_code == 200
    items = r.json()
    mine = next(
        (it for it in items if it.get("username", "").startswith("nochip_")),
        None,
    )
    assert mine is not None
    assert mine.get("equipped_title") is None


@pytest.mark.asyncio
async def test_recent_liquidations_includes_equipped_title(client):
    """强平事件主用户装备了 title 时，列表行返回 equipped_title chip。"""
    uid, tid, sfx = await _mk_user_with_equipped_title("liqtop")
    async with async_session_maker() as s:
        async with s.begin():
            evt = LiquidationEvent(
                user_id=uid,
                triggered_at=datetime.now(timezone.utc),
                pre_cash=Decimal("100"),
                pre_debt=Decimal("200"),
                pre_holdings_value=Decimal("50"),
                pre_net_worth=Decimal("-50"),
                pre_margin_ratio=Decimal("-0.25"),
                sold_positions_count=1,
                total_proceeds=Decimal("50"),
                repaid_amount=Decimal("50"),
                remaining_debt=Decimal("150"),
                post_cash=Decimal("100"),
                trigger_source="scheduler",
                mode="emergency",
            )
            s.add(evt)
    r = await client.get("/api/v1/loan/recent-liquidations")
    assert r.status_code == 200, r.text
    items = r.json()
    mine = next(
        (
            it for it in items
            if it.get("user_id") == uid
            or it.get("username", "").startswith("liqtop_")
        ),
        None,
    )
    assert mine is not None, f"User {uid} not in {items}"
    assert "equipped_title" in mine
    assert mine["equipped_title"] is not None
    assert mine["equipped_title"]["color"] == "#FFD700"
    assert mine["equipped_title"]["id"] == tid


@pytest.mark.asyncio
async def test_recent_liquidations_no_equipped_returns_null(client):
    """没装备 title 的用户：equipped_title 应为 null（不缺这个 key）。"""
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(
                username=f"liqplain_{suffix}",
                email=f"lp{suffix}@x.com",
                casdoor_id=f"lp_{suffix}",
                cash=Decimal("100"),
            )
            s.add(u)
            await s.flush()
            uid = u.id
            evt = LiquidationEvent(
                user_id=uid,
                triggered_at=datetime.now(timezone.utc),
                pre_cash=Decimal("0"),
                pre_debt=Decimal("10"),
                pre_holdings_value=Decimal("0"),
                pre_net_worth=Decimal("-10"),
                pre_margin_ratio=Decimal("-1"),
                sold_positions_count=0,
                total_proceeds=Decimal("0"),
                repaid_amount=Decimal("0"),
                remaining_debt=Decimal("10"),
                post_cash=Decimal("0"),
                trigger_source="scheduler",
                mode="emergency",
            )
            s.add(evt)
    r = await client.get("/api/v1/loan/recent-liquidations")
    assert r.status_code == 200
    items = r.json()
    mine = next(
        (it for it in items if it.get("user_id") == uid),
        None,
    )
    assert mine is not None
    assert "equipped_title" in mine
    assert mine["equipped_title"] is None
