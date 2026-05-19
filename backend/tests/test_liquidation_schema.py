"""Schema 改动单元测：TransactionType 新值 + User 新字段 + LiquidationEvent 表存在。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.models.base import (
    LiquidationEvent,
    TransactionType,
    User,
)


def test_transaction_type_has_liquidate():
    """TransactionType 必须有 LIQUIDATE 枚举值。"""
    assert TransactionType.LIQUIDATE == "liquidate"
    assert "liquidate" in {t.value for t in TransactionType}


def test_user_has_last_liquidated_at_field():
    """User 必须有 last_liquidated_at: Optional[datetime] 字段。"""
    assert "last_liquidated_at" in User.model_fields
    field = User.model_fields["last_liquidated_at"]
    assert field.default is None


@pytest.mark.asyncio
async def test_liquidation_event_can_be_created(client):
    """LiquidationEvent 表能写入 + 查询。"""
    from app.core.database import async_session_maker
    async with async_session_maker() as db:
        u = User(username="liq_test", casdoor_id="liq_test_cas",
                 cash=Decimal("100"), debt=Decimal("500"))
        db.add(u)
        await db.commit()
        await db.refresh(u)

        ev = LiquidationEvent(
            user_id=u.id,
            triggered_at=datetime.now(timezone.utc),
            pre_cash=Decimal("100"),
            pre_debt=Decimal("500"),
            pre_holdings_value=Decimal("200"),
            pre_net_worth=Decimal("-200"),
            pre_margin_ratio=Decimal("-0.4"),
            sold_positions_count=2,
            total_proceeds=Decimal("180"),
            repaid_amount=Decimal("180"),
            remaining_debt=Decimal("320"),
            post_cash=Decimal("0"),
            trigger_source="scheduler",
        )
        db.add(ev)
        await db.commit()
        await db.refresh(ev)

        assert ev.id is not None
        assert ev.user_id == u.id
        assert ev.pre_margin_ratio == Decimal("-0.4")
        assert ev.trigger_source == "scheduler"
