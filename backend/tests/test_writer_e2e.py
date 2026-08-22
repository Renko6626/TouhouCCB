"""writer 路径 E2E：flag 开启时端到端可用，证明 flag 开启时端到端可用。"""
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlmodel import select

from app.core.database import async_session_maker
from app.models.base import Outcome, OutcomeCandle, Position, Transaction
from app.services.candle_flusher import CANDLE_FLUSHER
from app.services.market_writer import WRITER
from app.services.site_config import clear_cache


@pytest_asyncio.fixture
async def writer_on(client):
    """启用 writer（模拟「flag=true 后重启」：显式 start）。"""
    async with async_session_maker() as s:
        await s.execute(text(
            "INSERT INTO siteconfig (key, value, value_type, updated_at) "
            "VALUES ('single_writer_enabled', 'true', 'bool', CURRENT_TIMESTAMP) "
            "ON CONFLICT (key) DO UPDATE SET value='true'"))
        await s.commit()
    clear_cache()
    await WRITER.start()
    yield
    await WRITER.stop()
    CANDLE_FLUSHER._pending.clear()


async def _dev_login(client, username: str) -> dict:
    resp = await client.post("/api/v1/auth/dev-login", json={"username": username})
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _create_market(client, admin_headers: dict, title: str) -> tuple[int, list[int]]:
    resp = await client.post(
        "/api/v1/market/create",
        headers=admin_headers,
        json={
            "title": title,
            "description": "",
            "liquidity_b": 100.0,
            "outcomes": ["yes", "no"],
        },
    )
    assert resp.status_code in (200, 201), resp.text
    mid = resp.json()["market_id"]
    async with async_session_maker() as s:
        outs = (await s.execute(
            select(Outcome).where(Outcome.market_id == mid).order_by(Outcome.id.asc())
        )).scalars().all()
    return mid, [o.id for o in outs]


@pytest.mark.asyncio
async def test_e2e_buy_sell_via_api_uses_writer(client, writer_on):
    # 第一个 dev-login 用户自动成为超管，正好用它建市场
    admin_headers = await _dev_login(client, "admin_e2e")
    mid, oids = await _create_market(client, admin_headers, "e2e market")

    # 市场已注册进 writer
    st = WRITER.get_state(mid)
    assert st is not None, "create_market 应把新市场注册进 WRITER"

    user_headers = await _dev_login(client, "user_e2e")

    # 1. HTTP buy → 200 + 响应字段契约零变更
    resp = await client.post(
        "/api/v1/market/buy",
        headers=user_headers,
        json={"outcome_id": oids[0], "shares": 10, "accept_any_slippage": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body.keys()) == {"shares", "cost", "new_cash", "pay", "message"}

    # 2. WRITER 内存状态已推进（证明走的是 writer 而不是老路径）
    st = WRITER.get_state(mid)
    assert st.q_dec[0] == Decimal("10.000000")

    # 3. DB Transaction/Position 落库
    async with async_session_maker() as s:
        o = await s.get(Outcome, oids[0])
        assert o.total_shares == Decimal("10.000000")
        pos = (await s.execute(
            select(Position).where(Position.outcome_id == oids[0])
        )).scalars().first()
        assert pos is not None and pos.amount == Decimal("10.000000")
        txs = (await s.execute(select(Transaction))).scalars().all()
        assert len(txs) == 1

    # 4. candle 还在 flusher pending，交易后 OutcomeCandle 表应为空
    async with async_session_maker() as s:
        rows = (await s.execute(select(OutcomeCandle))).scalars().all()
    assert rows == [], "candle 应先进 flusher pending，未落库"

    # 5. flush 一次后出现 8 行（2 outcome × 4 档）
    await CANDLE_FLUSHER.flush_once()
    async with async_session_maker() as s:
        rows = (await s.execute(select(OutcomeCandle))).scalars().all()
    assert len(rows) == 8

    # 6. HTTP sell → 200，state 回退
    resp = await client.post(
        "/api/v1/market/sell",
        headers=user_headers,
        json={"outcome_id": oids[0], "shares": 4, "accept_any_slippage": True},
    )
    assert resp.status_code == 200, resp.text
    st = WRITER.get_state(mid)
    assert st.q_dec[0] == Decimal("6.000000")


@pytest.mark.asyncio
async def test_e2e_flag_off_uses_old_path(client):
    # 不开 writer_on：flag 关闭
    assert WRITER.enabled is False

    admin_headers = await _dev_login(client, "admin_old")
    mid, oids = await _create_market(client, admin_headers, "old path market")
    # create_market 不注册进 writer（flag off）
    assert WRITER.get_state(mid) is None

    user_headers = await _dev_login(client, "user_old")

    resp = await client.post(
        "/api/v1/market/buy",
        headers=user_headers,
        json={"outcome_id": oids[0], "shares": 5, "accept_any_slippage": True},
    )
    assert resp.status_code == 200, resp.text

    # 老路径事务内 UPSERT candle：交易后立刻有行，无需 flusher.flush_once()
    async with async_session_maker() as s:
        rows = (await s.execute(select(OutcomeCandle))).scalars().all()
    assert len(rows) == 8, "老路径应在事务内直接落 candle，不经 flusher pending"
