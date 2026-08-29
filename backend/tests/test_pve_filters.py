"""is_bot 过滤集成：排行榜开关 / 财富统计开关 / bot_detection 扫描排除。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import (
    BotSuspicion, Market, MarketStatus, Outcome, SiteConfig,
    Transaction, TransactionType, User,
)


async def _seed_user(username_prefix: str, cash: str, is_bot: bool = False) -> int:
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(
                username=f"{username_prefix}_{suffix}",
                casdoor_id=None if is_bot else f"cd_{suffix}",
                cash=Decimal(cash), is_bot=is_bot,
            )
            s.add(u)
            await s.flush()
            return u.id


async def _set_config(key: str, value: str, value_type: str = "bool"):
    async with async_session_maker() as s:
        async with s.begin():
            row = (await s.execute(select(SiteConfig).where(SiteConfig.key == key))).scalars().first()
            if row:
                row.value = value
            else:
                s.add(SiteConfig(key=key, value=value, value_type=value_type))
    from app.services.site_config import clear_cache
    clear_cache()
    from app.api.v1.market import clear_leaderboard_cache
    clear_leaderboard_cache()


# ── 排行榜开关 ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_leaderboard_includes_bots_by_default(client):
    await _seed_user("human", "100")
    bot_id = await _seed_user("npcbot", "1000", is_bot=True)
    r = await client.get("/api/v1/market/leaderboard?mode=net_worth")
    ids = [it["user_id"] for it in r.json()]
    assert bot_id in ids and ids[0] == bot_id  # 默认参与且钱多排第一


@pytest.mark.asyncio
async def test_leaderboard_excludes_bots_when_switched_off(client):
    human_id = await _seed_user("human", "100")
    bot_id = await _seed_user("npcbot", "1000", is_bot=True)
    await _set_config("leaderboard_include_bots", "false")
    r = await client.get("/api/v1/market/leaderboard?mode=net_worth")
    ids = [it["user_id"] for it in r.json()]
    assert bot_id not in ids and human_id in ids


# ── 财富统计开关 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wealth_stats_bot_switch(client):
    admin_id = await _seed_user("adm", "0")
    async with async_session_maker() as s:
        async with s.begin():
            u = (await s.execute(select(User).where(User.id == admin_id))).scalars().one()
            u.is_superuser = True
    h = {"Authorization": f"Bearer {create_access_token(admin_id)}"}
    await _seed_user("human", "100")
    await _seed_user("npcbot", "1000", is_bot=True)

    r = await client.get("/api/v1/admin/stats/wealth", headers=h)
    assert r.json()["user_count"] == 3  # 默认含机器人（admin 自己也是 user）

    await _set_config("wealth_stats_include_bots", "false")
    r2 = await client.get("/api/v1/admin/stats/wealth", headers=h)
    assert r2.json()["user_count"] == 2


# ── bot_detection 扫描排除 ──────────────────────────────────────────────


async def _seed_txns(user_id: int, n: int):
    async with async_session_maker() as s:
        async with s.begin():
            m = Market(title=f"f_m_{uuid.uuid4().hex[:6]}", description="",
                       liquidity_b=100.0, status=MarketStatus.TRADING, tags="")
            s.add(m)
            await s.flush()
            o = Outcome(market_id=m.id, label="A", total_shares=Decimal("0"))
            s.add(o)
            await s.flush()
            now = datetime.now(timezone.utc)
            for i in range(n):
                s.add(Transaction(
                    user_id=user_id, outcome_id=o.id,
                    type=TransactionType.BUY, shares=Decimal("1"),
                    cost=Decimal("10"), price=Decimal("0.5"),
                    gross=Decimal("10"), fee=Decimal("0"),
                    timestamp=now - timedelta(minutes=i),
                ))


@pytest.mark.asyncio
async def test_bot_detection_skips_is_bot_users(client):
    """同样的高频行为：真人触发预警，is_bot 机器人不触发。"""
    from app.services.loan_migrate import auto_migrate
    from app.services import bot_detection

    await auto_migrate()  # seed anti-bot 默认配置行
    for k, v in [
        ("bot_detection_enabled", "true"),
        ("bot_detection_window_sec", "7200"),
        ("bot_freq_threshold", "5"),
        ("bot_late_night_threshold", "999"),
        ("bot_interval_stddev_ms_threshold", "1"),
        ("bot_fast_follow_trigger_cost", "999999.0"),
        ("bot_fast_follow_latency_ms", "1000"),
        ("bot_fast_follow_count_threshold", "999"),
    ]:
        await _set_config(k, v, value_type="int" if v.isdigit() else "decimal")

    human_id = await _seed_user("human", "1000")
    bot_id = await _seed_user("npcbot", "1000", is_bot=True)
    await _seed_txns(human_id, 10)
    await _seed_txns(bot_id, 10)

    result = await bot_detection.run_bot_detection_once()
    assert result["triggered_count"] >= 1
    async with async_session_maker() as s:
        rows = (await s.execute(select(BotSuspicion))).scalars().all()
        flagged = {r.user_id for r in rows}
        assert human_id in flagged
        assert bot_id not in flagged, "官方 PvE 机器人不该被自己的反作弊抓"
