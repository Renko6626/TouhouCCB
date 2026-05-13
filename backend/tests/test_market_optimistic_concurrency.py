"""
乐观并发优化（P2）回归测试

优化目标：把 buy/sell 事务持锁时间从 ~1s 压到 ~50ms：
  Phase 1：无锁快照读 + Python LMSR 计算（不持锁）
  Phase 2：短暂持锁写入；快照失效则自动重试（最多 MAX_RETRIES 次），超限返回 503
"""
import re
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import AsyncClient, ASGITransport
from sqlmodel import SQLModel

import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.main import app
from app.core.database import engine, async_session_maker
from app.core.users import create_access_token
from app.models.base import Market, MarketStatus, Outcome, User


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
        await conn.run_sync(SQLModel.metadata.create_all)


@pytest_asyncio.fixture
async def client():
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost:8005") as ac:
            yield ac


async def _make_user(cash=Decimal("100000")):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(
                username=f"u_{suffix}",
                email=f"{suffix}@t.com",
                casdoor_id=f"cd_{suffix}",
                cash=cash,
                debt=Decimal("0"),
                is_superuser=False,
            )
            s.add(u)
            await s.flush()
            uid = u.id
    token = create_access_token(uid)
    return uid, {"Authorization": f"Bearer {token}"}


async def _make_market(liquidity_b: float = 10000.0, n_outcomes: int = 2):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            m = Market(
                title=f"m_{suffix}",
                liquidity_b=liquidity_b,
                status=MarketStatus.TRADING,
            )
            s.add(m)
            await s.flush()
            outcome_ids = []
            for i in range(n_outcomes):
                o = Outcome(market_id=m.id, label=f"opt_{i}", total_shares=Decimal("0"))
                s.add(o)
                await s.flush()
                outcome_ids.append(o.id)
    return m.id, outcome_ids


def _get_fn_body(text: str, fn_name: str) -> str:
    m = re.search(rf"async def {fn_name}\b", text)
    assert m, f"找不到 {fn_name} 函数定义"
    start = m.start()
    next_fn = re.search(r"\n(@router|async def )", text[start + 1:])
    end = start + 1 + next_fn.start() if next_fn else len(text)
    return text[start:end]


_MARKET_SRC = Path(__file__).resolve().parent.parent / "app" / "api" / "v1" / "market.py"


# ────────────────────────────────────────────────
# 单元测试：_snapshot_still_valid 辅助函数
# ────────────────────────────────────────────────

def test_snapshot_valid_when_shares_unchanged():
    """快照 shares 与锁定后 outcome.total_shares 完全一致时返回 True。"""
    from app.api.v1.market import _snapshot_still_valid

    o1 = SimpleNamespace(total_shares=Decimal("10.000000"))
    o2 = SimpleNamespace(total_shares=Decimal("5.000000"))

    snapshot = [Decimal("10.000000"), Decimal("5.000000")]
    assert _snapshot_still_valid(snapshot, [o1, o2]) is True


def test_snapshot_invalid_when_any_share_changed():
    """任意一个 outcome.total_shares 与快照不一致时返回 False。"""
    from app.api.v1.market import _snapshot_still_valid

    o1 = SimpleNamespace(total_shares=Decimal("10.000001"))  # 并发写入后份额增加了
    o2 = SimpleNamespace(total_shares=Decimal("5.000000"))

    snapshot = [Decimal("10.000000"), Decimal("5.000000")]
    assert _snapshot_still_valid(snapshot, [o1, o2]) is False


# ────────────────────────────────────────────────
# 源码级断言：LMSR 计算在加锁之前
# ────────────────────────────────────────────────

def test_buy_calculates_lmsr_before_acquiring_lock():
    """
    buy_shares 的 calculate_lmsr_cost 必须出现在 _lock_market 之前。
    把 Python 计算移到持锁临界区外是压缩持锁时间的核心手段。
    """
    body = _get_fn_body(_MARKET_SRC.read_text(encoding="utf-8"), "buy_shares")
    calc_pos = body.find("calculate_lmsr_cost(")
    lock_pos = body.find("_lock_market(")
    assert calc_pos != -1, "buy_shares 中缺失 calculate_lmsr_cost 调用"
    assert lock_pos != -1, "buy_shares 中缺失 _lock_market 调用"
    assert calc_pos < lock_pos, (
        f"buy_shares：calculate_lmsr_cost (pos={calc_pos}) 在 _lock_market "
        f"(pos={lock_pos}) 之后——LMSR 计算仍在持锁区内，优化未生效"
    )


def test_sell_calculates_lmsr_before_acquiring_lock():
    """sell_shares 的 calculate_lmsr_cost 必须出现在 _lock_market 之前。"""
    body = _get_fn_body(_MARKET_SRC.read_text(encoding="utf-8"), "sell_shares")
    calc_pos = body.find("calculate_lmsr_cost(")
    lock_pos = body.find("_lock_market(")
    assert calc_pos != -1, "sell_shares 中缺失 calculate_lmsr_cost 调用"
    assert lock_pos != -1, "sell_shares 中缺失 _lock_market 调用"
    assert calc_pos < lock_pos, (
        f"sell_shares：calculate_lmsr_cost (pos={calc_pos}) 在 _lock_market "
        f"(pos={lock_pos}) 之后——LMSR 计算仍在持锁区内，优化未生效"
    )


# ────────────────────────────────────────────────
# 源码级断言：重试结构存在
# ────────────────────────────────────────────────

def test_buy_has_retry_loop():
    """buy_shares 必须有以 MAX_RETRIES 为上限的重试循环。"""
    body = _get_fn_body(_MARKET_SRC.read_text(encoding="utf-8"), "buy_shares")
    assert "MAX_RETRIES" in body, "buy_shares 中缺失 MAX_RETRIES 重试上限"


def test_sell_has_retry_loop():
    """sell_shares 必须有以 MAX_RETRIES 为上限的重试循环。"""
    body = _get_fn_body(_MARKET_SRC.read_text(encoding="utf-8"), "sell_shares")
    assert "MAX_RETRIES" in body, "sell_shares 中缺失 MAX_RETRIES 重试上限"


def test_buy_calls_snapshot_valid():
    """buy_shares 必须调用 _snapshot_still_valid 进行乐观冲突检测。"""
    body = _get_fn_body(_MARKET_SRC.read_text(encoding="utf-8"), "buy_shares")
    assert "_snapshot_still_valid" in body, "buy_shares 缺失 _snapshot_still_valid 快照校验"


def test_sell_calls_snapshot_valid():
    """sell_shares 必须调用 _snapshot_still_valid 进行乐观冲突检测。"""
    body = _get_fn_body(_MARKET_SRC.read_text(encoding="utf-8"), "sell_shares")
    assert "_snapshot_still_valid" in body, "sell_shares 缺失 _snapshot_still_valid 快照校验"


# ────────────────────────────────────────────────
# 功能回归：无竞争场景正常成交
# ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_buy_succeeds_normally(client):
    """无竞争时 buy 应正常返回 200。"""
    _, h = await _make_user()
    _, outcome_ids = await _make_market()
    r = await client.post(
        "/api/v1/market/buy",
        json={"outcome_id": outcome_ids[0], "shares": 1},
        headers=h,
    )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_sell_succeeds_normally(client):
    """无竞争时 sell 应正常返回 200。"""
    _, h = await _make_user()
    _, outcome_ids = await _make_market()
    r = await client.post(
        "/api/v1/market/buy",
        json={"outcome_id": outcome_ids[0], "shares": 1},
        headers=h,
    )
    assert r.status_code == 200, r.text
    r = await client.post(
        "/api/v1/market/sell",
        json={"outcome_id": outcome_ids[0], "shares": 1},
        headers=h,
    )
    assert r.status_code == 200, r.text


# ────────────────────────────────────────────────
# 功能测试：重试机制
# ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_buy_retries_and_succeeds_when_snapshot_stale_once(client):
    """
    _snapshot_still_valid 首次返回 False（模拟并发写入使快照失效），
    第二次返回 True → buy_shares 应重试并最终返回 200。
    """
    _, h = await _make_user()
    _, outcome_ids = await _make_market()

    call_count = {"n": 0}

    def fake_valid(snapshot, locked_outcomes):
        call_count["n"] += 1
        return call_count["n"] > 1  # 第一次 False，之后 True

    with patch("app.api.v1.market._snapshot_still_valid", side_effect=fake_valid):
        r = await client.post(
            "/api/v1/market/buy",
            json={"outcome_id": outcome_ids[0], "shares": 1},
            headers=h,
        )

    assert r.status_code == 200, r.text
    assert call_count["n"] >= 2, "预期至少调用 2 次（首次 stale + 一次重试）"


@pytest.mark.asyncio
async def test_buy_returns_503_when_all_retries_stale(client):
    """_snapshot_still_valid 持续返回 False → 耗尽 MAX_RETRIES 后返回 503。"""
    _, h = await _make_user()
    _, outcome_ids = await _make_market()

    with patch("app.api.v1.market._snapshot_still_valid", return_value=False):
        r = await client.post(
            "/api/v1/market/buy",
            json={"outcome_id": outcome_ids[0], "shares": 1},
            headers=h,
        )

    assert r.status_code == 503, r.text


@pytest.mark.asyncio
async def test_sell_retries_and_succeeds_when_snapshot_stale_once(client):
    """sell 路径：_snapshot_still_valid 首次 False，第二次 True → 重试成功。"""
    _, h = await _make_user()
    _, outcome_ids = await _make_market()
    r = await client.post(
        "/api/v1/market/buy",
        json={"outcome_id": outcome_ids[0], "shares": 1},
        headers=h,
    )
    assert r.status_code == 200

    call_count = {"n": 0}

    def fake_valid(snapshot, locked_outcomes):
        call_count["n"] += 1
        return call_count["n"] > 1

    with patch("app.api.v1.market._snapshot_still_valid", side_effect=fake_valid):
        r = await client.post(
            "/api/v1/market/sell",
            json={"outcome_id": outcome_ids[0], "shares": 1},
            headers=h,
        )

    assert r.status_code == 200, r.text
    assert call_count["n"] >= 2


@pytest.mark.asyncio
async def test_sell_returns_503_when_all_retries_stale(client):
    """sell 路径：_snapshot_still_valid 持续 False → 503。"""
    _, h = await _make_user()
    _, outcome_ids = await _make_market()
    r = await client.post(
        "/api/v1/market/buy",
        json={"outcome_id": outcome_ids[0], "shares": 1},
        headers=h,
    )
    assert r.status_code == 200

    with patch("app.api.v1.market._snapshot_still_valid", return_value=False):
        r = await client.post(
            "/api/v1/market/sell",
            json={"outcome_id": outcome_ids[0], "shares": 1},
            headers=h,
        )

    assert r.status_code == 503, r.text
