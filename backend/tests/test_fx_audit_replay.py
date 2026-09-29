from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.base import User
from app.models.fx import FxPair, FxTrade, FxTreasury, FxWallet
from app.services import audit_replay
from app.services.fx import scheduler, trading
from app.services.fx.amm import marginal_price
from app.services.fx.engine import FxEngine
from tests.fx_test_helpers import add_pair, fx_db


async def _run_real_flow(fx_db, monkeypatch, code: str):
    """Create only production FX/audit rows, including both system directions."""
    pair, _ = await add_pair(fx_db, code=code, gold="100", foreign="100")
    pair.buy_fee_rate = Decimal("0.010000")
    pair.sell_fee_rate = Decimal("0.010000")
    await fx_db.commit()
    user = User(username=f"{code.lower()}-user", cash=Decimal("1000.000000"), debt=Decimal("0"),
                tos_accepted_at=datetime.now(timezone.utc))
    fx_db.add(user)
    await fx_db.commit()

    async def _noop(_trade):
        return None

    monkeypatch.setattr(trading, "publish_public_event", _noop)
    bought = await trading.execute_trade(
        fx_db, user.id, pair.id, "buy", Decimal("1.000000"), Decimal("0"), f"{code}-buy"
    )
    await trading.execute_trade(
        fx_db, user.id, pair.id, "sell", bought.output_amount, Decimal("0"), f"{code}-sell"
    )
    await scheduler.fund_pair(fx_db, pair.id, Decimal("1.000000"), Decimal("2.000000"), user.id)
    await scheduler.withdraw_pair(fx_db, pair.id, Decimal("0.500000"), Decimal("1.000000"), user.id)

    pair = await fx_db.get(FxPair, pair.id)
    treasury = (await fx_db.execute(select(FxTreasury).where(FxTreasury.pair_id == pair.id))).scalars().one()
    engine = FxEngine()
    price = marginal_price(pair.gold_reserve, pair.foreign_reserve)
    system_buy = await engine._system_move(
        fx_db, pair, treasury, price * Decimal("1.010000"), Decimal("100000.000000"),
        Decimal("100000.000000"), source="test-buy", now=datetime.now(timezone.utc),
    )
    assert system_buy and system_buy.trade.side == "buy"
    price = marginal_price(pair.gold_reserve, pair.foreign_reserve)
    system_sell = await engine._system_move(
        fx_db, pair, treasury, price * Decimal("0.990000"), Decimal("100000.000000"),
        Decimal("100000.000000"), source="test-sell", now=datetime.now(timezone.utc),
    )
    assert system_sell and system_sell.trade.side == "sell"
    await fx_db.commit()
    return pair.id, user.id


@pytest.mark.asyncio
async def test_fx_replay_real_trade_system_fund_withdraw_and_live_compare(fx_db, monkeypatch):
    pair_id, user_id = await _run_real_flow(fx_db, monkeypatch, "REALREPLAY")
    events = await audit_replay.load_events(fx_db)
    snap, mismatches = audit_replay.fold(events, check=True)
    live = await audit_replay.compare_with_live(fx_db, snap)
    trades = (await fx_db.execute(select(FxTrade).where(FxTrade.pair_id == pair_id))).scalars().all()
    assert {(trade.source.startswith("system_"), trade.side) for trade in trades} >= {
        (False, "buy"), (False, "sell"), (True, "buy"), (True, "sell")
    }
    assert {event.event_type for event in events} >= {"fx_trade", "fx_fund", "fx_withdraw"}
    assert any(event.user_id == user_id and event.payload["side"] == "sell" for event in events)
    assert mismatches == []
    assert live == []


@pytest.mark.asyncio
async def test_fx_replay_detects_event_and_live_tampering(fx_db, monkeypatch):
    pair_id, user_id = await _run_real_flow(fx_db, monkeypatch, "TAMPER")
    events = await audit_replay.load_events(fx_db)
    player_buy = next(event for event in events if event.event_type == "fx_trade"
                      and event.user_id == user_id and event.payload["side"] == "buy")
    player_buy.payload = {
        **player_buy.payload,
        "post_foreign_reserve": str(Decimal(player_buy.payload["post_foreign_reserve"]) + Decimal("1.000000")),
    }
    await fx_db.commit()
    _, mismatches = audit_replay.fold(events, check=True)
    assert any(m.event_id == player_buy.id and m.field == "post_foreign_reserve" for m in mismatches)

    player_buy.payload = {**player_buy.payload, "post_foreign_reserve": str(
        Decimal(player_buy.payload["post_foreign_reserve"]) - Decimal("1.000000")
    )}
    pair = await fx_db.get(FxPair, pair_id)
    treasury = (await fx_db.execute(select(FxTreasury).where(FxTreasury.pair_id == pair_id))).scalars().one()
    wallet = (await fx_db.execute(select(FxWallet).where(
        FxWallet.user_id == user_id, FxWallet.pair_id == pair_id
    ))).scalars().one()
    user = await fx_db.get(User, user_id)
    pair.gold_reserve += Decimal("1.000000")
    pair.foreign_reserve += Decimal("1.000000")
    treasury.gold_balance += Decimal("1.000000")
    treasury.foreign_balance += Decimal("1.000000")
    wallet.foreign_amount += Decimal("1.000000")
    user.cash += Decimal("1.000000")
    await fx_db.commit()
    events = await audit_replay.load_events(fx_db)
    snap, replay_mismatches = audit_replay.fold(events, check=True)
    live = await audit_replay.compare_with_live(fx_db, snap)
    assert replay_mismatches == []
    assert any(m.entity == f"fx_pair:{pair_id}" and m.field == "pool.gold" for m in live)
    assert any(m.entity == f"fx_pair:{pair_id}" and m.field == "pool.foreign" for m in live)
    assert any(m.entity == f"fx_pair:{pair_id}" and m.field == "treasury.gold" for m in live)
    assert any(m.entity == f"fx_pair:{pair_id}" and m.field == "treasury.foreign" for m in live)
    assert any(m.entity == f"fx_wallet:{user_id}:{pair_id}" and m.field == "foreign_amount" for m in live)
    assert any(m.entity == f"user:{user_id}" and m.field == "cash" for m in live)
