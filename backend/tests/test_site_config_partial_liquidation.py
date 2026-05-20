import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from decimal import Decimal
import pytest
from app.core.database import async_session_maker
from app.services import site_config


PARTIAL_LIQ_KEYS = [
    "liquidation_partial_pct",
    "liquidation_target_margin",
    "liquidation_emergency_threshold",
]


@pytest.mark.asyncio
async def test_partial_liquidation_configs_seeded_with_defaults(client):
    from app.services.loan_migrate import auto_migrate
    # setup_db 已 drop_all + create_all，需重新 auto_migrate 才有默认值
    await auto_migrate()

    async with async_session_maker() as s:
        for k in PARTIAL_LIQ_KEYS:
            row = await site_config._fetch(s, k)
            assert row is not None, f"missing default config: {k}"

    async with async_session_maker() as s:
        assert (await site_config.get_decimal(s, "liquidation_partial_pct")) == Decimal("0.10")
        assert (await site_config.get_decimal(s, "liquidation_target_margin")) == Decimal("0.30")
        assert (await site_config.get_decimal(s, "liquidation_emergency_threshold")) == Decimal("0.05")
