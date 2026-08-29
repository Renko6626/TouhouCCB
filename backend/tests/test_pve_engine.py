"""PvE 引擎集成测：机器人经回环 HTTP 走真实 buy/sell 路径（JWT 签发、
anti-bot 通过、Transaction/资金落库），以及死亡判定与全局限速护栏。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import ASGITransport
from sqlalchemy import select

from app.core.database import async_session_maker
from app.main import app
from app.models.base import (
    Market, MarketStatus, Outcome, SiteConfig, Transaction, User,
)
from app.models.bot import BotProfile
from app.services.pve.client import LoopbackTrader
from app.services.pve.engine import PveEngine

PAST = datetime.now(timezone.utc) - timedelta(seconds=5)


def _engine() -> PveEngine:
    return PveEngine(trader=LoopbackTrader(transport=ASGITransport(app=app)))


async def _seed_config(**overrides):
    values = {"pve_enabled": ("bool", "true"), **{
        k: ("int" if k.endswith(("cap", "bps", "tick")) else "decimal", str(v))
        for k, v in overrides.items()
    }}
    async with async_session_maker() as s:
        async with s.begin():
            for k, (vt, v) in values.items():
                s.add(SiteConfig(key=k, value=v, value_type=vt))
    from app.services.site_config import clear_cache
    clear_cache()


async def _seed_market() -> int:
    async with async_session_maker() as s:
        async with s.begin():
            m = Market(title=f"pve_m_{uuid.uuid4().hex[:6]}", description="",
                       liquidity_b=100.0, status=MarketStatus.TRADING, tags="")
            s.add(m)
            await s.flush()
            for label in ("A", "B"):
                s.add(Outcome(market_id=m.id, label=label, total_shares=Decimal("0")))
            return m.id


async def _seed_bot(template: str, params: dict, cash: str = "100") -> tuple[int, int]:
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=f"pvebot_{uuid.uuid4().hex[:6]}", is_bot=True,
                     cash=Decimal(cash))
            s.add(u)
            await s.flush()
            p = BotProfile(user_id=u.id, template=template, params=params)
            s.add(p)
            await s.flush()
            return p.id, u.id


def _force_wake(engine: PveEngine):
    for rt in engine.runtimes.values():
        rt.next_action_at = PAST


HODLER_PARAMS = {
    "skip_prob": 0.0, "buy_cny_min": 5, "buy_cny_max": 5,
    "check_interval_sec": 60, "active_preset": "always",
}


@pytest.mark.asyncio
async def test_disabled_engine_is_noop(client):
    engine = _engine()
    result = await engine.tick()
    assert result == {"enabled": False}
    await engine.trader.close()


@pytest.mark.asyncio
async def test_bot_trades_through_real_api(client):
    await _seed_config()
    await _seed_market()
    pid, uid = await _seed_bot("hodler", HODLER_PARAMS)

    engine = _engine()
    r1 = await engine.tick()  # 第一轮：进调度（首次延迟错峰，不一定唤醒）
    assert r1["bots"] == 1
    _force_wake(engine)
    r2 = await engine.tick()
    assert r2["trade"] == 1, f"应成交一笔：{r2} / log={engine.get_log(pid)}"

    async with async_session_maker() as s:
        txs = (await s.execute(select(Transaction).where(Transaction.user_id == uid))).scalars().all()
        assert len(txs) == 1 and txs[0].type == "buy"
        user = (await s.execute(select(User).where(User.id == uid))).scalars().one()
        assert user.cash < Decimal("100")  # 真金白银花出去了
        profile = (await s.execute(select(BotProfile).where(BotProfile.id == pid))).scalars().one()
        assert profile.last_trade_at is not None
    assert any(e["event"] == "trade" for e in engine.get_log(pid))
    await engine.trader.close()


@pytest.mark.asyncio
async def test_broke_bot_marked_dead(client):
    await _seed_config()
    await _seed_market()
    pid, uid = await _seed_bot("hodler", HODLER_PARAMS, cash="0.5")  # < 默认水位 3

    engine = _engine()
    await engine.tick()
    _force_wake(engine)
    r = await engine.tick()
    assert r["dead"] == 1 and r["trade"] == 0
    async with async_session_maker() as s:
        profile = (await s.execute(select(BotProfile).where(BotProfile.id == pid))).scalars().one()
        assert profile.status == "dead"
    assert pid not in engine.runtimes
    assert any("死亡" in e["msg"] for e in engine.get_log(pid))
    await engine.trader.close()


@pytest.mark.asyncio
async def test_global_orders_per_min_cap(client):
    await _seed_config()
    async with async_session_maker() as s:
        async with s.begin():
            s.add(SiteConfig(key="pve_orders_per_min_cap", value="0", value_type="int"))
    from app.services.site_config import clear_cache
    clear_cache()
    await _seed_market()
    pid, uid = await _seed_bot("hodler", HODLER_PARAMS)

    engine = _engine()
    await engine.tick()
    _force_wake(engine)
    r = await engine.tick()
    assert r["trade"] == 0 and r["skip"] == 1
    async with async_session_maker() as s:
        txs = (await s.execute(select(Transaction).where(Transaction.user_id == uid))).scalars().all()
        assert txs == []
    assert any("上限" in e["msg"] for e in engine.get_log(pid))
    await engine.trader.close()


@pytest.mark.asyncio
async def test_believer_trades_through_real_api(client):
    """believer 系（fan 预设）经回环 HTTP 真实下单：信念高于市价 → 买入本命。"""
    await _seed_config()
    await _seed_market()
    pid, uid = await _seed_bot("fan", {
        "skip_prob": 0.0, "shock_prob": 0.0, "conviction": 0.3, "w_swing": 0.0,
        "check_interval_sec": 60, "active_preset": "always",
    })
    engine = _engine()
    await engine.tick()
    _force_wake(engine)
    r = await engine.tick()
    assert r["trade"] == 1, f"应成交一笔：{r} / log={engine.get_log(pid)}"
    async with async_session_maker() as s:
        txs = (await s.execute(select(Transaction).where(Transaction.user_id == uid))).scalars().all()
        assert len(txs) == 1 and txs[0].type == "buy"
    await engine.trader.close()


def test_collect_wakees_release_count_is_randomized():
    """积压削峰不能是固定节律（每 tick 精确放行 cap 个=规律脉冲）——
    放行数在 [cap/2, cap] 内随机。"""
    import random as _random
    from datetime import datetime, timedelta, timezone as _tz

    from app.services.pve.engine import Runtime
    from app.services.pve.templates import TEMPLATE_REGISTRY
    from tests.pve_helpers import make_view

    engine = PveEngine()
    now = datetime.now(_tz.utc)
    for pid in range(40):  # 40 个全部到点的机器人
        engine.runtimes[pid] = Runtime(
            profile_id=pid, user_id=pid, username=f"b{pid}",
            template=TEMPLATE_REGISTRY["hodler"](),
            params=dict(TEMPLATE_REGISTRY["hodler"].default_params),
            market_scope=None,
            next_action_at=now - timedelta(seconds=5),
            rng=_random.Random(pid),
        )
    cfg = {"max_wakes_per_tick": 10}
    sizes = {len(engine._collect_wakees(make_view(), cfg, now)) for _ in range(60)}
    assert all(5 <= s <= 10 for s in sizes)
    assert len(sizes) > 1, "放行数恒定——脉冲仍有固定节律"


@pytest.mark.asyncio
async def test_tick_evolves_activity(client):
    """潮汐：tick 演化全局活跃度并在 snapshot 暴露（默认幅度 >0）。"""
    await _seed_config()
    engine = _engine()
    vals = set()
    for _ in range(10):
        await engine.tick()
        vals.add(engine.activity)
    assert len(vals) > 1, "activity 没有演化"
    assert all(0.3 <= v <= 1.9 for v in vals)
    assert "activity" in engine.snapshot()
    await engine.trader.close()


@pytest.mark.asyncio
async def test_market_view_carries_sentiment(client):
    """site_config `pve_sentiment` → 解析进 MarketView.sentiment（风向注入的接线）。"""
    from app.models.base import Outcome
    from app.services.pve.market_view import build_market_view

    await _seed_config()
    mid = await _seed_market()
    async with async_session_maker() as s:
        async with s.begin():
            oid = (await s.execute(
                select(Outcome.id).where(Outcome.market_id == mid).order_by(Outcome.id)
            )).scalars().first()
            s.add(SiteConfig(key="pve_sentiment", value='{"tilts": {"%d": 0.2}}' % oid,
                             value_type="string"))
    from app.services.site_config import clear_cache
    clear_cache()
    async with async_session_maker() as s:
        view = await build_market_view(s)
    assert view.sentiment == {oid: 0.2}


@pytest.mark.asyncio
async def test_paused_bot_leaves_schedule(client):
    await _seed_config()
    await _seed_market()
    pid, _uid = await _seed_bot("hodler", HODLER_PARAMS)
    engine = _engine()
    await engine.tick()
    assert pid in engine.runtimes
    async with async_session_maker() as s:
        async with s.begin():
            profile = (await s.execute(select(BotProfile).where(BotProfile.id == pid))).scalars().one()
            profile.status = "paused"
    await engine.tick()
    assert pid not in engine.runtimes
    assert any("移出调度" in e["msg"] for e in engine.get_log(pid))
    await engine.trader.close()
