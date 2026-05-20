import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from datetime import datetime, timezone
from decimal import Decimal
import pytest

from app.core.database import async_session_maker
from app.models.base import LiquidationEvent, User


@pytest.mark.asyncio
async def test_liquidation_event_mode_default_is_emergency(client):
    """新加的 mode 字段默认 'emergency'（兼容历史 row 语义）。"""
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username="liq_mode_test", casdoor_id="liq_mode_cas")
            s.add(u)
            await s.flush()
            ev = LiquidationEvent(
                user_id=u.id,
                triggered_at=datetime.now(timezone.utc),
                pre_cash=Decimal("100"), pre_debt=Decimal("1000"),
                pre_holdings_value=Decimal("50"),
                pre_net_worth=Decimal("-850"),
                pre_margin_ratio=Decimal("-0.85"),
                sold_positions_count=0,
                total_proceeds=Decimal("0"),
                repaid_amount=Decimal("0"),
                remaining_debt=Decimal("1000"),
                post_cash=Decimal("100"),
                trigger_source="scheduler",
            )
            s.add(ev)
            await s.flush()
            assert ev.mode == "emergency"


@pytest.mark.asyncio
async def test_liquidation_event_mode_can_be_partial(client):
    """显式传 mode='partial' 能保存。"""
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username="liq_mode_test2", casdoor_id="liq_mode_cas2")
            s.add(u)
            await s.flush()
            ev = LiquidationEvent(
                user_id=u.id,
                triggered_at=datetime.now(timezone.utc),
                pre_cash=Decimal("100"), pre_debt=Decimal("1000"),
                pre_holdings_value=Decimal("950"),
                pre_net_worth=Decimal("50"),
                pre_margin_ratio=Decimal("0.05"),
                sold_positions_count=1,
                total_proceeds=Decimal("100"),
                repaid_amount=Decimal("100"),
                remaining_debt=Decimal("900"),
                post_cash=Decimal("100"),
                trigger_source="scheduler",
                mode="partial",
            )
            s.add(ev)
            await s.flush()
            assert ev.mode == "partial"
