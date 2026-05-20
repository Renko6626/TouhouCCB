import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
from app.core.database import async_session_maker
from app.services import site_config
from app.services.site_config import SiteConfigError


ANTI_BOT_KEYS = [
    "activity_mode_enabled",
    "quant_whitelist_user_ids",
    "bot_detection_enabled",
    "bot_detection_interval_sec",
    "bot_detection_window_sec",
    "bot_freq_threshold",
    "bot_late_night_threshold",
    "bot_interval_stddev_ms_threshold",
    "bot_fast_follow_trigger_cost",
    "bot_fast_follow_latency_ms",
    "bot_fast_follow_count_threshold",
]


@pytest.mark.asyncio
async def test_anti_bot_configs_seeded_with_defaults(client):
    """启动 lifespan 后 11 个 anti-bot config keys 应该都存在 + 默认值正确。"""
    from app.services.loan_migrate import auto_migrate
    # setup_db 已 drop_all + create_all，需重新 auto_migrate 才有默认值
    await auto_migrate()

    async with async_session_maker() as s:
        for k in ANTI_BOT_KEYS:
            try:
                row = await site_config._fetch(s, k)
            except SiteConfigError:
                pytest.fail(f"missing default config: {k}")

    # 抽几个默认值核对
    async with async_session_maker() as s:
        assert (await site_config.get_bool(s, "activity_mode_enabled")) is False
        assert (await site_config.get_bool(s, "bot_detection_enabled")) is True
        assert (await site_config.get_int(s, "bot_detection_interval_sec")) == 1800
        assert (await site_config.get_int(s, "bot_detection_window_sec")) == 7200


@pytest.mark.asyncio
async def test_anti_bot_keys_in_whitelist():
    """11 个 anti-bot key 必须在 _WHITELIST 中才能通过 admin API 改。"""
    from app.api.v1.site_config import _WHITELIST
    for k in ANTI_BOT_KEYS:
        assert k in _WHITELIST, f"missing from _WHITELIST: {k}"
