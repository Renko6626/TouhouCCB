from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.base import User
from app.models.fx import FxPair, FxTrade, FxTreasury, FxWallet
from app.services import audit_replay
from app.services.fx import liquidity, trading
from tests.fx_test_helpers import add_pair, fx_db


async def _run_real_flow(fx_db, monkeypatch, code: str):
    """Create production trades and liquidity audit rows."""
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
    await liquidity.fund_pair(fx_db, pair.id, Decimal("1.000000"), Decimal("2.000000"), user.id)
    await liquidity.withdraw_pair(fx_db, pair.id, Decimal("0.500000"), Decimal("1.000000"), user.id)

    return pair.id, user.id


@pytest.mark.asyncio
async def test_fx_replay_real_trade_fund_withdraw_and_live_compare(fx_db, monkeypatch):
    pair_id, user_id = await _run_real_flow(fx_db, monkeypatch, "REALREPLAY")
    events = await audit_replay.load_events(fx_db)
    snap, mismatches = audit_replay.fold(events, check=True)
    live = await audit_replay.compare_with_live(fx_db, snap)
    trades = (await fx_db.execute(select(FxTrade).where(FxTrade.pair_id == pair_id))).scalars().all()
    assert {(trade.source.startswith("system_"), trade.side) for trade in trades} >= {
        (False, "buy"), (False, "sell")
    }
    assert {event.event_type for event in events} >= {"fx_trade", "fx_fund", "fx_withdraw"}
    assert any(event.user_id == user_id and event.payload["side"] == "sell" for event in events)
    assert mismatches == []
    assert live == []

    # Fixed legacy entries retain old monetary replay without reviving the engine.
    legacy = [
        AuditEvent(id=100, event_type="fx_trade", ref_id=99, payload={
            "pair_id":99, "source":"system_target", "side":"buy",
            "input_amount":"10", "output_amount":"5", "fee_amount":"0",
            "pre_gold_reserve":"100", "pre_foreign_reserve":"100",
            "post_gold_reserve":"110", "post_foreign_reserve":"95",
            "treasury_after":{"gold_balance":"90", "foreign_balance":"105"}}),
        AuditEvent(id=101, event_type="fx_trade", ref_id=99, payload={
            "pair_id":99, "source":"system_event", "side":"sell",
            "input_amount":"5", "output_amount":"10", "fee_amount":"0",
            "pre_gold_reserve":"110", "pre_foreign_reserve":"95",
            "post_gold_reserve":"100", "post_foreign_reserve":"100",
            "treasury_after":{"gold_balance":"100", "foreign_balance":"100"}}),
    ]
    historical, errors = audit_replay.fold(legacy, check=True)
    assert errors == []
    assert historical.fx_pairs[99].gold == historical.fx_pairs[99].foreign == Decimal("100")
    assert historical.fx_pairs[99].treasury_gold == historical.fx_pairs[99].treasury_foreign == Decimal("100")


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
