import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from decimal import Decimal
import pytest
from app.core.database import async_session_maker
from app.services import site_config


@pytest.mark.asyncio
async def test_partial_liquidation_configs_seeded_with_defaults(client):
    from app.services.loan_migrate import auto_migrate
    # setup_db 已 drop_all + create_all，需重新 auto_migrate 才有默认值
    await auto_migrate()

    async with async_session_maker() as s:
        assert (await site_config.get_decimal(s, "liquidation_partial_pct")) == Decimal("0.10")
