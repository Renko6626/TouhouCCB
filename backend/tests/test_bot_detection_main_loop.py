import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.base import (
    BotSuspicion, Market, MarketStatus, Outcome, SiteConfig,
    Transaction, TransactionType, User,
)
from app.services import bot_detection


async def _seed_anti_bot_defaults():
    """setup_db 每个 case 都 drop_all → 默认 site_config rows 没了；这里 re-seed。"""
    from app.services.loan_migrate import auto_migrate
    await auto_migrate()


async def _enable_detection_with_low_threshold():
    """改 site_config 让阈值低，方便测触发 high_freq。"""
    await _seed_anti_bot_defaults()
    async with async_session_maker() as s:
        async with s.begin():
            for k, v in [
                ("bot_detection_enabled", "true"),
                ("bot_detection_window_sec", "7200"),
                ("bot_freq_threshold", "5"),  # 低到 5 笔就触发
                ("bot_late_night_threshold", "999"),
                ("bot_interval_stddev_ms_threshold", "1"),
                ("bot_fast_follow_trigger_cost", "999999.0"),
                ("bot_fast_follow_latency_ms", "1000"),
                ("bot_fast_follow_count_threshold", "999"),
            ]:
                row = (await s.execute(
                    select(SiteConfig).where(SiteConfig.key == k)
                )).scalars().first()
                if row:
                    row.value = v
    from app.services.site_config import clear_cache
    clear_cache()


async def _seed_user_with_n_recent_txns(n: int):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=f"bd_{suffix}", casdoor_id=f"bd_cas_{suffix}",
                     cash=Decimal("1000"))
            s.add(u)
            await s.flush()
            m = Market(title=f"bd_m_{suffix}", description="",
                       liquidity_b=100.0, status=MarketStatus.TRADING, tags="")
            s.add(m)
            await s.flush()
            o = Outcome(market_id=m.id, label="A", total_shares=Decimal("0"))
            s.add(o)
            await s.flush()
            now = datetime.now(timezone.utc)
            for i in range(n):
                tx = Transaction(
                    user_id=u.id, outcome_id=o.id,
                    type=TransactionType.BUY, shares=Decimal("1"),
                    cost=Decimal("10"), price=Decimal("0.5"),
                    gross=Decimal("10"), fee=Decimal("0"),
                    timestamp=now - timedelta(minutes=i),
                )
                s.add(tx)
            return u.id


@pytest.mark.asyncio
async def test_main_loop_triggers_signal_writes_bot_suspicion(client):
    await _enable_detection_with_low_threshold()
    uid = await _seed_user_with_n_recent_txns(10)

    result = await bot_detection.run_bot_detection_once()
    assert result["enabled"] is True
    assert result["scanned_users"] >= 1
    assert result["triggered_count"] >= 1

    async with async_session_maker() as s:
        rows = (await s.execute(
            select(BotSuspicion).where(BotSuspicion.user_id == uid)
        )).scalars().all()
        assert len(rows) == 1
        assert "high_freq" in rows[0].signals
        assert rows[0].review_status == "pending"


@pytest.mark.asyncio
async def test_main_loop_skips_whitelist_user(client):
    await _enable_detection_with_low_threshold()
    uid = await _seed_user_with_n_recent_txns(10)
    async with async_session_maker() as s:
        async with s.begin():
            row = (await s.execute(
                select(SiteConfig).where(SiteConfig.key == "quant_whitelist_user_ids")
            )).scalars().first()
            row.value = str(uid)
    from app.services.site_config import clear_cache
    clear_cache()

    await bot_detection.run_bot_detection_once()

    async with async_session_maker() as s:
        rows = (await s.execute(
            select(BotSuspicion).where(BotSuspicion.user_id == uid)
        )).scalars().all()
        assert len(rows) == 0, "whitelist user 不应写 BotSuspicion"


@pytest.mark.asyncio
async def test_main_loop_dedup_within_6h(client):
    """6h 内同 user 同信号已存在 pending → 不重写。"""
    await _enable_detection_with_low_threshold()
    uid = await _seed_user_with_n_recent_txns(10)

    # 第一次跑：写一条
    await bot_detection.run_bot_detection_once()
    async with async_session_maker() as s:
        rows = (await s.execute(
            select(BotSuspicion).where(BotSuspicion.user_id == uid)
        )).scalars().all()
        assert len(rows) == 1

    # 第二次立刻跑：6h dedup 应跳过
    await bot_detection.run_bot_detection_once()
    async with async_session_maker() as s:
        rows = (await s.execute(
            select(BotSuspicion).where(BotSuspicion.user_id == uid)
        )).scalars().all()
        assert len(rows) == 1, "6h dedup 失败，重复写了"


@pytest.mark.asyncio
async def test_main_loop_disabled_returns_skip_with_full_fields(client):
    """detection_enabled=false → 早返回但字段集完整 (review I-1 教训)。"""
    await _seed_anti_bot_defaults()
    async with async_session_maker() as s:
        async with s.begin():
            row = (await s.execute(
                select(SiteConfig).where(SiteConfig.key == "bot_detection_enabled")
            )).scalars().first()
            row.value = "false"
    from app.services.site_config import clear_cache
    clear_cache()

    result = await bot_detection.run_bot_detection_once()
    expected_keys = {"enabled", "scanned_users", "triggered_count",
                     "skipped_count", "duration_ms"}
    assert set(result.keys()) >= expected_keys
    assert result["enabled"] is False
