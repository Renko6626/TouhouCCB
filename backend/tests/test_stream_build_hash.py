"""snapshot 携带前端 build hash（阶段 2 build 版本自刷机制）。"""
from decimal import Decimal

import pytest
import pytest_asyncio

from app.api.v1.stream import _build_snapshot
from app.core.database import async_session_maker
from app.models.base import Market, MarketStatus, Outcome
from app.services.market_writer import WRITER


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    # 拷贝自 test_writer_buy.py：conftest 的 autouse setup_db 已在每测试前
    # drop+create 全表，这里只负责 WRITER 生命周期收尾（本文件不启动 WRITER，
    # stop() 在未 start 时是 no-op，保留是为了与写路径测试保持同一收尾习惯）。
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


@pytest.mark.asyncio
async def test_snapshot_carries_build_sha(monkeypatch):
    monkeypatch.setenv("APP_BUILD_SHA", "abc123")
    mid, _ = await _seed_market()
    from app.core.database import async_session_maker
    async with async_session_maker() as db:
        snap = await _build_snapshot(db, mid)
    assert snap["frontend_build"] == "abc123"


@pytest.mark.asyncio
async def test_snapshot_build_sha_defaults_empty(monkeypatch):
    monkeypatch.delenv("APP_BUILD_SHA", raising=False)
    mid, _ = await _seed_market()
    from app.core.database import async_session_maker
    async with async_session_maker() as db:
        snap = await _build_snapshot(db, mid)
    assert snap["frontend_build"] == ""
