"""writer → tick 帧接线测试：形状一致 / 双发开关 / 价格不动点 / 强平空帧。"""
import json
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlmodel import select

from app.core.database import async_session_maker
from app.models.base import (
    Market, MarketStatus, Outcome, Position, Transaction, TransactionType, User,
)
from app.services.lmsr import calculate_lmsr_with_prices, quantize_price
from app.services.market_writer import WRITER
from app.services.realtime import BROKER
from app.services.tick_broadcaster import TICK_BROADCASTER
from app.services.writer_ops import BuyCmd, CloseCmd, LiquidateMarketCmd, ResolveCmd


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    # conftest 的 autouse setup_db 已在每测试前 drop+create 全表（并 seed 好
    # activity_mode_enabled 等 site_config），且它先于本 fixture 执行——这里
    # 不再重复 drop/create（否则会把 setup_db 刚 seed 的行冲掉，parity 测试的
    # HTTP 买入走 verify_anti_bot 读 activity_mode_enabled 会 500）。
    # 只负责本文件专属的 WRITER 生命周期收尾。
    yield
    await WRITER.stop()


async def _seed_market(status=MarketStatus.TRADING, shares=("3.5", "0")) -> tuple[int, list[int]]:
    async with async_session_maker() as s:
        m = Market(title="t", description="", liquidity_b=100.0, status=status, tags="")
        s.add(m)
        await s.flush()
        oids = []
        for v in shares:
            o = Outcome(market_id=m.id, label=f"o{v}", total_shares=Decimal(v))
            s.add(o)
            await s.flush()
            oids.append(o.id)
        await s.commit()
        return m.id, oids


async def _seed_user(cash="1000") -> int:
    async with async_session_maker() as s:
        u = User(username="alice", casdoor_id="cas_alice", cash=Decimal(cash))
        s.add(u)
        await s.flush()
        uid = u.id
        await s.commit()
        return uid


def _buy(mid, oid, uid, shares="10", **kw):
    return BuyCmd(market_id=mid, outcome_id=oid, user_id=uid, username="alice",
                  shares=Decimal(shares), max_cost=kw.get("max_cost"),
                  max_slippage_bps=kw.get("max_slippage_bps"),
                  accept_any_slippage=kw.get("accept_any_slippage", False))


def _parse_sse(blob: bytes) -> dict:
    text = blob.decode("utf-8")
    assert text.endswith("\n\n")
    data_line = next(l for l in text.split("\n") if l.startswith("data: "))
    return json.loads(data_line[len("data: "):])


TRADE_KEYS = {"id", "type", "outcome_id", "username", "shares", "price", "gross",
              "fee", "post_market_price", "market_prices_post", "timestamp"}


@pytest.fixture(autouse=True)
def _reset_tick():
    TICK_BROADCASTER._pending.clear()
    yield


async def _drain_frames(sub):
    await TICK_BROADCASTER.flush_once()
    out = []
    while not sub.q.empty():
        out.append(_parse_sse(sub.q.get_nowait()))
    return out


@pytest.mark.asyncio
async def test_buy_emits_tick_frame_and_legacy_event_with_identical_trade_payload():
    """双发默认开：一笔 buy → 1 条 legacy trade + 1 帧 tick，trade payload 逐字段相同（MIN-11）。"""
    mid, oids = await _seed_market(shares=("0", "0"))
    uid = await _seed_user()
    await WRITER.start()
    sub, _ = await BROKER.subscribe(mid)
    try:
        await WRITER.submit(_buy(mid, oids[0], uid, shares="10", accept_any_slippage=True))
        payloads = await _drain_frames(sub)
        legacy = [p for p in payloads if p["type"] == "trade"]
        ticks = [p for p in payloads if p["type"] == "tick"]
        assert len(legacy) == 1 and len(ticks) == 1
        legacy_trade = legacy[0]["data"]["trade"]
        tick_trade = ticks[0]["data"]["trades"][0]
        assert set(legacy_trade.keys()) == TRADE_KEYS
        assert tick_trade == legacy_trade                       # 同一 payload 对象双投
        # seq 连续（共用计数器）
        seqs = sorted(p["seq"] for p in payloads)
        assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))
        # 帧价格 = writer state 的 8dp 量化
        st = WRITER.get_state(mid)
        assert ticks[0]["data"]["prices"] == [float(quantize_price(p)) for p in st.prices]
        assert ticks[0]["data"]["status"] == "trading"
    finally:
        await BROKER.unsubscribe(mid, sub)


@pytest.mark.asyncio
async def test_legacy_off_only_tick(monkeypatch):
    mid, oids = await _seed_market(shares=("0", "0"))
    uid = await _seed_user()
    await WRITER.start()

    async def _off():
        return False
    monkeypatch.setattr("app.services.tick_broadcaster.legacy_events_enabled", _off)

    sub, _ = await BROKER.subscribe(mid)
    try:
        await WRITER.submit(_buy(mid, oids[0], uid, shares="5", accept_any_slippage=True))
        payloads = await _drain_frames(sub)
        assert [p["type"] for p in payloads] == ["tick"]
    finally:
        await BROKER.unsubscribe(mid, sub)


@pytest.mark.asyncio
async def test_prices_fixpoint_after_buy():
    """MIN-1：apply 后 st.prices 恒等于由量化后 q 重新导出的价格。"""
    mid, oids = await _seed_market(shares=("3.5", "0"))
    uid = await _seed_user()
    await WRITER.start()
    await WRITER.submit(_buy(mid, oids[0], uid, shares="7", accept_any_slippage=True))
    st = WRITER.get_state(mid)
    _, derived = calculate_lmsr_with_prices(st.q, st.b)
    assert st.prices == derived        # 列表逐元素严格相等，不是近似


@pytest.mark.asyncio
async def test_close_frame_carries_status_and_resolve_carries_settlement():
    mid, oids = await _seed_market(shares=("2", "0"))
    await WRITER.start()
    sub, _ = await BROKER.subscribe(mid)
    try:
        await WRITER.submit(CloseCmd(market_id=mid))
        frames = [p for p in await _drain_frames(sub) if p["type"] == "tick"]
        assert frames[-1]["data"]["status"] == "halt"

        from app.services.writer_ops import ResumeCmd
        await WRITER.submit(ResumeCmd(market_id=mid))
        await _drain_frames(sub)

        await WRITER.submit(ResolveCmd(market_id=mid, winning_outcome_id=oids[0],
                                       payout=Decimal("1"), admin_id=1))
        frames = [p for p in await _drain_frames(sub) if p["type"] == "tick"]
        settled = frames[-1]["data"]
        assert settled["status"] == "settled"
        assert settled["settlement"]["winning_outcome_id"] == oids[0]
        assert "settled_at" in settled["settlement"]
    finally:
        await BROKER.unsubscribe(mid, sub)


@pytest.mark.asyncio
async def test_liquidation_emits_price_frame_with_empty_trades():
    """强平改价但无成交事件 → 空 trades 帧把新价格推出去（改进现状：老架构强平不发 SSE）。"""
    mid, oids = await _seed_market(shares=("0", "0"))
    uid = await _seed_user(cash="1000")
    await WRITER.start()
    await WRITER.submit(_buy(mid, oids[0], uid, shares="20", accept_any_slippage=True))
    await TICK_BROADCASTER.flush_once()     # 清掉 buy 的残帧（在 sub 订阅前 flush，无人接收）
    sub, _ = await BROKER.subscribe(mid)
    try:
        res = await WRITER.submit(LiquidateMarketCmd(
            market_id=mid, user_id=uid, mode="emergency", partial_pct=Decimal("1")))
        assert res["sold_count"] == 1
        frames = [p for p in await _drain_frames(sub) if p["type"] == "tick"]
        assert len(frames) == 1
        assert frames[0]["data"]["trades"] == []
        st = WRITER.get_state(mid)
        assert frames[0]["data"]["prices"] == [float(quantize_price(p)) for p in st.prices]
    finally:
        await BROKER.unsubscribe(mid, sub)
