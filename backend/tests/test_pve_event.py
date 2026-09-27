"""活动短线：白天连续看盘、分市场编制、重仓信号与参数隔离。"""
import random
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.base import Market, MarketStatus, User
from app.models.bot import BotProfile
from app.services.pve.attention import next_wake
from app.services.pve.engine import PveEngine
from app.services.pve.templates import BelieverTemplate, TradeBrief, shares_for_budget
from tests.pve_helpers import NOW, make_bot, make_view
from tests.test_admin_pve import admin_headers  # noqa: F401

BASE = "/api/v1/admin/pve"


async def _markets():
    async with async_session_maker() as db:
        async with db.begin():
            rows = [Market(title=f"event-{i}", description="", status=MarketStatus.TRADING,
                           liquidity_b=b, tags="") for i, b in enumerate((3000, 10000, 30000))]
            db.add_all(rows)
            await db.flush()
            return [m.id for m in rows]


@pytest.mark.asyncio
async def test_event_generation_spreads_50_funded_bots_over_three_markets(client, admin_headers):
    mids = await _markets()
    response = await client.post(f"{BASE}/bots/generate", headers=admin_headers, json={
        "items": [{"template": "chaser", "count": 12}, {"template": "sheep", "count": 12},
                  {"template": "swinger", "count": 12}, {"template": "bottom_fisher", "count": 9},
                  {"template": "fan", "count": 5}],
        "activity_mode": "event", "initial_cash": "500", "market_scope": mids,
    })
    assert response.status_code == 200, response.text
    assert len(response.json()["created"]) == 50
    async with async_session_maker() as db:
        profiles = (await db.execute(select(BotProfile))).scalars().all()
        users = (await db.execute(select(User).where(User.is_bot.is_(True)))).scalars().all()
    assert all(u.cash == Decimal("500") for u in users)
    assert Counter(tuple(p.market_scope) for p in profiles) == {
        (mids[0],): 17, (mids[1],): 17, (mids[2],): 16,
    }
    # catches generation silently dropping event mode or re-sampling night-only schedules
    now = datetime(2026, 9, 28, 2, tzinfo=timezone.utc)  # 北京时间 10:00
    for profile in profiles:
        for seed in range(20):
            delay = (next_wake(now, profile.params, random.Random(seed)) - now).total_seconds()
            assert 180 <= delay <= 300


@pytest.mark.parametrize("pace", [0.35, 1.0, 1.8])
def test_event_regular_checks_stay_three_to_five_minutes_despite_tides(pace):
    now = datetime(2026, 9, 28, 2, tzinfo=timezone.utc)
    params = {"activity_mode": "event", "check_interval_sec": 240, "active_preset": "always"}
    delays = [(next_wake(now, params, random.Random(i), pace=pace) - now).total_seconds()
              for i in range(300)]
    assert all(180 <= d <= 300 for d in delays)
    assert len(set(delays)) > 250  # catches fixed periodic batches


def test_shortline_edge_scale_makes_a_conviction_trade_material_in_a_deep_market():
    view = make_view(liquidity_b=10000)
    common = dict(outcome_id=11, conviction=0.12, w_swing=0.0, skip_prob=0.0,
                  shock_prob=0.0, yolo_prob=0.0, aggressiveness=0.6, max_bet_frac=0.75)
    regular = BelieverTemplate().decide(make_bot(BelieverTemplate, cash="500", **common), view)
    event = BelieverTemplate().decide(
        make_bot(BelieverTemplate, cash="500", edge_scale=0.025, **common), view)
    assert regular is not None and event is not None
    assert event.side == regular.side == "buy"
    assert event.shares > regular.shares * 2
    assert event.shares <= Decimal(str(shares_for_budget(375, 0.5, 10000)))


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"activity_mode": "event", "market_scope": None},
    {"activity_mode": "event", "market_scope": [999999]},
    {"activity_mode": "event", "market_scope": [1], "items": [{"template": "grid", "count": 1}]},
])
async def test_invalid_event_generation_does_not_create_accounts(client, admin_headers, payload):
    await _markets()
    request = {"items": [{"template": "chaser", "count": 1}], "initial_cash": "500", **payload}
    response = await client.post(f"{BASE}/bots/generate", headers=admin_headers, json=request)
    assert response.status_code == 400, response.text
    async with async_session_maker() as db:
        assert (await db.execute(select(BotProfile))).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{"check_interval_sec": None}, {"check_interval_sec": "abc"},
                                   {"edge_scale": 0}, {"skip_prob": 2}, {"lookback_min": -1},
                                   {"active_preset": []}, {"active_preset": {}}])
async def test_bad_attention_and_strategy_params_are_rejected(client, admin_headers, params):
    response = await client.post(f"{BASE}/bots/generate", headers=admin_headers, json={
        "items": [{"template": "chaser", "count": 1}], "initial_cash": "500",
    })
    pid = response.json()["created"][0]["profile_id"]
    response = await client.patch(f"{BASE}/bots/{pid}", headers=admin_headers, json={"params": params})
    assert response.status_code == 400, response.text


@pytest.mark.asyncio
async def test_one_existing_bad_profile_cannot_block_the_healthy_pool(client, admin_headers):
    response = await client.post(f"{BASE}/bots/generate", headers=admin_headers, json={
        "items": [{"template": "chaser", "count": 2}], "initial_cash": "500",
    })
    bad, good = [b["profile_id"] for b in response.json()["created"]]
    async with async_session_maker() as db:
        async with db.begin():
            profile = await db.get(BotProfile, bad)
            profile.params = {"check_interval_sec": None}
    engine = PveEngine()
    async with async_session_maker() as db:
        await engine._sync_runtimes(db, datetime.now(timezone.utc))
    assert set(engine.runtimes) == {good}


@pytest.mark.asyncio
async def test_negative_wake_cap_is_rejected(client, admin_headers):
    response = await client.put(f"{BASE}/config", headers=admin_headers,
                                json={"pve_max_wakes_per_tick": "-1"})
    assert response.status_code == 400, response.text


@pytest.mark.parametrize("identity,learns", [(False, True), (True, False), (None, False)])
def test_sustained_bias_updates_anchor_only_with_multiple_real_buyers(identity, learns):
    bot = make_bot(BelieverTemplate, outcome_id=11, conviction=0, skip_prob=0,
                   shock_prob=0, herd_coef=0, w_swing=0, anchor_adapt_rate=0.2)
    template = BelieverTemplate()
    template.decide(bot, make_view())
    for minute in (4, 8, 12, 16, 20, 24, 28):
        now = NOW + timedelta(minutes=minute)
        trades = [TradeBrief(now - timedelta(minutes=1), 11, 1, "buy", 100, 0.7, [0.7, 0.3],
                             user_id=uid, is_bot=identity) for uid in (101, 102, 103)]
        view = make_view(price_a=0.7, price_b=0.3, trades=trades)
        view.now = now
        template.decide(bot, view)
    anchor = bot.memory["belief_anchor"]
    if learns:
        assert anchor[11] > 0.6  # 真正提高长线立场，而不只跟涨当下看法
        assert anchor[12] < 0.4
    else:
        assert anchor == {11: 0.5, 12: 0.5}  # 自家 bot 的成交不能自证新事实


def test_brief_human_buying_spike_does_not_rewrite_longterm_belief():
    bot = make_bot(BelieverTemplate, outcome_id=11, conviction=0, skip_prob=0,
                   shock_prob=0, herd_coef=0, w_swing=0, anchor_adapt_rate=0.2)
    template = BelieverTemplate()
    template.decide(bot, make_view())
    trades = [TradeBrief(NOW, 11, 1, "buy", 100, 0.7, [0.7, 0.3], user_id=uid, is_bot=False)
              for uid in (101, 102, 103)]
    template.decide(bot, make_view(price_a=0.7, price_b=0.3, trades=trades))
    assert bot.memory["belief_anchor"] == {11: 0.5, 12: 0.5}


def test_event_fan_remains_stubborn_while_other_personas_can_learn():
    from app.services.pve.service import spawn_params
    from app.services.pve.templates import FanTemplate
    assert spawn_params("fan", random.Random(3), "event")["anchor_adapt_rate"] == 0
    assert spawn_params("chaser", random.Random(3), "event")["anchor_adapt_rate"] > 0
    bot = make_bot(FanTemplate, outcome_id=11, conviction=0, skip_prob=0,
                   shock_prob=0, herd_coef=0)
    template = FanTemplate()
    template.decide(bot, make_view())
    for minute in range(4, 45, 4):
        view = make_view(price_a=0.7, price_b=0.3)
        view.now = NOW + timedelta(minutes=minute)
        view.trades = [TradeBrief(view.now, 11, 1, "buy", 100, 0.7, [0.7, 0.3], user_id=uid, is_bot=False)
                       for uid in (101, 102, 103)]
        template.decide(bot, view)
    assert bot.memory["belief_anchor"] == {11: 0.5, 12: 0.5}


def test_generated_event_fan_keeps_its_anchor_for_a_full_activity():
    from app.services.pve.service import spawn_params
    from app.services.pve.templates import FanTemplate
    params = spawn_params("fan", random.Random(3), "event")
    bot = make_bot(FanTemplate, seed=7, **params)
    template = FanTemplate()
    template.decide(bot, make_view())
    original = dict(bot.memory["belief_anchor"])
    for minute in range(4, 601, 4):
        view = make_view(price_a=0.7, price_b=0.3)
        view.now = NOW + timedelta(minutes=minute)
        template.decide(bot, view)
    assert bot.memory["belief_anchor"] == original


def test_first_large_trade_still_creates_momentum_for_both_sides():
    trade = TradeBrief(NOW, 11, 1, "buy", 100, 0.6, [0.7, 0.3])
    trade.pre_market_price = 0.5
    view = make_view(price_a=0.7, price_b=0.3, trades=[trade])
    assert view.window_change(11, 10) == pytest.approx(0.2)
    assert view.window_change(12, 10) == pytest.approx(-0.2)


@pytest.mark.asyncio
async def test_market_snapshot_distinguishes_real_users_from_official_bots(client, admin_headers):
    from httpx import ASGITransport
    from app.main import app
    from app.models.base import Outcome
    from app.services.pve.client import LoopbackTrader
    from app.services.pve.market_view import build_market_view
    from tests.test_admin_pve import _seed_user
    from tests.test_pve_engine import _seed_market

    mid = await _seed_market()
    response = await client.post(f"{BASE}/bots/generate", headers=admin_headers, json={
        "items": [{"template": "chaser", "count": 1}], "initial_cash": "500",
        "market_scope": [mid], "activity_mode": "event",
    })
    bot_uid = response.json()["created"][0]["user_id"]
    human_uid = await _seed_user()
    async with async_session_maker() as db:
        async with db.begin():
            human = await db.get(User, human_uid)
            human.cash = Decimal("500")
            oid = (await db.execute(select(Outcome.id).where(Outcome.market_id == mid))).scalars().first()
    trader = LoopbackTrader(transport=ASGITransport(app=app))
    try:
        await trader.buy(human_uid, oid, Decimal("20"), 2500)
        await trader.buy(bot_uid, oid, Decimal("20"), 2500)
    finally:
        await trader.close()
    async with async_session_maker() as db:
        view = await build_market_view(db)
    assert {trade.user_id: trade.is_bot for trade in view.trades} == {human_uid: False, bot_uid: True}


@pytest.mark.asyncio
async def test_busy_market_keeps_older_human_sells_for_belief_evidence(client):
    from app.models.base import Outcome, Transaction
    from app.services.pve.market_view import build_market_view
    from tests.test_admin_pve import _seed_user
    from tests.test_pve_engine import _seed_market

    mid = await _seed_market()
    human = await _seed_user()
    bot_uid = await _seed_user()
    now = datetime.now(timezone.utc)
    async with async_session_maker() as db:
        async with db.begin():
            (await db.get(User, bot_uid)).is_bot = True
            oid = (await db.execute(select(Outcome.id).where(Outcome.market_id == mid))).scalars().first()
            db.add_all([Transaction(user_id=bot_uid, outcome_id=oid, type="buy", shares=Decimal("1"),
                                   cost=Decimal("0.5"), price=Decimal("0.5"), timestamp=now - timedelta(seconds=1))
                        for _ in range(601)])
            db.add(Transaction(user_id=human, outcome_id=oid, type="sell", shares=Decimal("100"),
                               cost=Decimal("50"), price=Decimal("0.5"), timestamp=now - timedelta(minutes=14)))
    async with async_session_maker() as db:
        view = await build_market_view(db)
    assert len(view.trades) == 600
    evidence = getattr(view, "human_trades", None)
    assert evidence is not None
    assert [(trade.user_id, trade.side, trade.shares) for trade in evidence] == [(human, "sell", 100)]


def test_complete_human_evidence_overrides_truncated_recent_buying():
    bot = make_bot(BelieverTemplate, outcome_id=11, conviction=0, skip_prob=0,
                   shock_prob=0, herd_coef=0, w_swing=0, anchor_adapt_rate=0.2)
    template = BelieverTemplate()
    template.decide(bot, make_view())
    for minute in (4, 8, 12, 16, 20, 24, 28):
        now = NOW + timedelta(minutes=minute)
        buys = [TradeBrief(now, 11, 1, "buy", 10, 0.7, [0.7, 0.3], user_id=uid, is_bot=False)
                for uid in (101, 102, 103)]
        sell = TradeBrief(now - timedelta(minutes=14), 11, 1, "sell", 100, 0.7, [0.7, 0.3],
                          user_id=104, is_bot=False)
        view = make_view(price_a=0.7, price_b=0.3, trades=buys)
        view.now = now
        view.human_trades = [*buys, sell]
        template.decide(bot, view)
    assert bot.memory["belief_anchor"] == {11: 0.5, 12: 0.5}
