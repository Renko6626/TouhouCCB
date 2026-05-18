"""liquidation_service.liquidate_user 单元测。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.base import (
    LiquidationEvent, Market, MarketStatus, Outcome,
    Position, Transaction, TransactionType, User,
)
from app.services.market_locks import lock_user
from app.services import liquidation_service


async def _setup_user_with_positions(db, *, cash, debt, positions):
    """positions: list of (outcome_label, amount, market_total_for_that_outcome).
    返回 (user, market, outcomes)。
    """
    u = User(username=f"liq_t_{cash}_{debt}", casdoor_id=f"liq_cas_{cash}_{debt}",
             cash=Decimal(str(cash)), debt=Decimal(str(debt)),
             debt_last_accrued_at=datetime.now(timezone.utc) if debt > 0 else None)
    db.add(u); await db.flush()

    m = Market(title="m", description="", liquidity_b=100.0,
               status=MarketStatus.TRADING, tags="")
    db.add(m); await db.flush()

    outs = []
    for label, amt, total_shares in positions:
        o = Outcome(market_id=m.id, label=label,
                    total_shares=Decimal(str(total_shares)))
        db.add(o); await db.flush()
        outs.append(o)
        if amt > 0:
            p = Position(user_id=u.id, outcome_id=o.id,
                         amount=Decimal(str(amt)),
                         cost_basis=Decimal("0"))
            db.add(p)
    await db.commit()
    return u, m, outs


@pytest.mark.asyncio
async def test_liquidate_user_happy_path_full_repay(client):
    """user 借 50，仓位卖光 >50 → 还光 debt，剩余 cash 留下。

    LMSR 参数：outcome A 200 shares（偏向 A 的市场），outcome B 100 shares，b=100。
    卖出 100 shares of A → proceeds ≈ 62；cash 10+62=72 > debt 50 → full repay。
    """
    async with async_session_maker() as db:
        u, m, outs = await _setup_user_with_positions(
            db, cash=10, debt=50,
            positions=[("A", 100, 200), ("B", 0, 100)],
        )
        user_id = u.id

    async with async_session_maker() as db:
        async with db.begin():
            user = await lock_user(db, user_id)
            event = await liquidation_service.liquidate_user(
                db, user, daily_rate=Decimal("0.01"), trigger_source="scheduler",
            )

    async with async_session_maker() as db:
        u2 = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        assert u2.debt == Decimal("0"), f"债务应清零, got {u2.debt}"
        assert u2.cash > Decimal("0"), f"卖出回款应 > 还债，剩点 cash, got {u2.cash}"
        assert u2.last_liquidated_at is not None

        positions = (await db.execute(
            select(Position).where(Position.user_id == user_id)
        )).scalars().all()
        assert len(positions) == 0, "所有 position 应已 delete"

        txs = (await db.execute(
            select(Transaction).where(Transaction.user_id == user_id)
        )).scalars().all()
        liq_txs = [t for t in txs if t.type == TransactionType.LIQUIDATE]
        assert len(liq_txs) == 1, "应有 1 笔 LIQUIDATE Transaction（B amount=0 跳过）"

        events = (await db.execute(
            select(LiquidationEvent).where(LiquidationEvent.user_id == user_id)
        )).scalars().all()
        assert len(events) == 1
        ev = events[0]
        assert ev.sold_positions_count == 1
        assert ev.repaid_amount > Decimal("0")
        assert ev.remaining_debt == Decimal("0")
        assert ev.trigger_source == "scheduler"


@pytest.mark.asyncio
async def test_liquidate_user_partial_repay_remaining_debt(client):
    """user 资不抵债：卖完仓 < debt → repay 部分，remaining_debt > 0。"""
    async with async_session_maker() as db:
        u, m, outs = await _setup_user_with_positions(
            db, cash=0, debt=500,
            positions=[("A", 5, 100)],
        )
        user_id = u.id

    async with async_session_maker() as db:
        async with db.begin():
            user = await lock_user(db, user_id)
            event = await liquidation_service.liquidate_user(
                db, user, daily_rate=Decimal("0.01"), trigger_source="admin_manual",
            )

    async with async_session_maker() as db:
        u2 = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        assert u2.debt > Decimal("0"), "卖完仍不够还，应剩 remaining debt"
        assert u2.cash == Decimal("0"), "所有 cash 应已用于还债"

        events = (await db.execute(
            select(LiquidationEvent).where(LiquidationEvent.user_id == user_id)
        )).scalars().all()
        ev = events[0]
        assert ev.remaining_debt > Decimal("0")
        assert ev.trigger_source == "admin_manual"


@pytest.mark.asyncio
async def test_liquidate_user_skips_settled_market(client):
    """已结算 market 的 position 应跳过（让 resolve flow 处理）。"""
    async with async_session_maker() as db:
        u, m, outs = await _setup_user_with_positions(
            db, cash=0, debt=100,
            positions=[("A", 10, 100)],
        )
        m.status = MarketStatus.SETTLED
        await db.commit()
        user_id = u.id

    async with async_session_maker() as db:
        async with db.begin():
            user = await lock_user(db, user_id)
            event = await liquidation_service.liquidate_user(
                db, user, daily_rate=Decimal("0.01"), trigger_source="scheduler",
            )

    async with async_session_maker() as db:
        positions = (await db.execute(
            select(Position).where(Position.user_id == user_id)
        )).scalars().all()
        assert len(positions) == 1, "settled market 的 position 应保留"

        events = (await db.execute(
            select(LiquidationEvent).where(LiquidationEvent.user_id == user_id)
        )).scalars().all()
        assert events[0].sold_positions_count == 0
