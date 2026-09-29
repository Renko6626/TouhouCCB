"""WP8a：F8 管理端 trading pair 上限（≤3）+ F9 reduce_only 显式运营开关。

覆盖验收：
- 第 4 个 trading pair 创建/切换返回 409；上限只在 admin 层，非 trading pair 数量不受限；
- 计数检查与写入在同一事务锁内，并发创建不会凑出第 4 个 trading pair；
- 公开 pair 列表返回全部非 draft（多 pair）；
- `reduce_only` 默认 false（既有 paused pair 仍"全停"），只有显式 PATCH 才翻转且留审计。
"""
from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.api.v1.admin_fx import router
from app.api.v1.fx import router as public_router
from app.core.database import get_async_session
from app.core.users import current_superuser
from app.models.audit import AuditEvent
from app.models.base import User
from app.models.fx import FxPair
from tests.fx_test_helpers import fx_db

PAIR = {"currency_code": "USD", "currency_name": "Dollar", "status": "trading",
        "gold_reserve": "100", "foreign_reserve": "100", "target_price": "1",
        "initial_price": "1", "target_min": "0.5", "target_max": "2"}

# 公开响应允许出现的 pair 字段（运营字段不得外露）。
PUBLIC_PAIR_FIELDS = {"id", "currency_code", "currency_name", "status",
                      "reduce_only", "pool_version", "created_at", "updated_at"}
OPERATOR_ONLY_FIELDS = {"gold_reserve", "foreign_reserve", "target_price", "initial_price",
                        "target_min", "target_max", "buy_fee_rate", "sell_fee_rate"}


def _pair(code: str, **overrides) -> dict:
    return {**PAIR, "currency_code": code, **overrides}


@pytest_asyncio.fixture
async def ctx(fx_db):
    admin = User(username="wp8a_admin", casdoor_id="wp8a_admin", is_superuser=True)
    fx_db.add(admin)
    await fx_db.commit()
    application = FastAPI()
    application.include_router(router, prefix="/api/v1/admin/fx")
    application.include_router(public_router, prefix="/api/v1/fx")

    async def session_override():
        yield fx_db

    async def admin_override():
        return admin

    application.dependency_overrides[get_async_session] = session_override
    application.dependency_overrides[current_superuser] = admin_override
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
        yield client, fx_db, admin


async def _trading_count(db) -> int:
    stmt = select(func.count()).select_from(FxPair).where(FxPair.status == "trading")
    return int((await db.execute(stmt)).scalar_one())


@pytest.mark.asyncio
async def test_three_trading_pairs_allowed_and_fourth_rejected(ctx):
    client, db, _ = ctx
    for code in ("AAA", "BBB", "CCC"):
        created = await client.post("/api/v1/admin/fx/pairs", json=_pair(code))
        assert created.status_code == 200, created.text
        assert created.json()["reduce_only"] is False
    assert await _trading_count(db) == 3

    fourth = await client.post("/api/v1/admin/fx/pairs", json=_pair("DDD"))
    assert fourth.status_code == 409, fourth.text
    assert "3" in fourth.json()["detail"]

    # 上限只是管理端运营限制：模型/schema 不写死品种数，非 trading pair 不受限。
    assert (await client.post("/api/v1/admin/fx/pairs",
                              json=_pair("EEE", status="paused"))).status_code == 200
    assert (await client.post("/api/v1/admin/fx/pairs",
                              json=_pair("FFF", status="draft"))).status_code == 200
    assert (await client.post("/api/v1/admin/fx/pairs",
                              json=_pair("GGG", status="closed"))).status_code == 200
    assert await _trading_count(db) == 3

    # 公开列表一次返回全部非 draft 的多个 pair（draft 隐藏；paused/closed 照旧可见）。
    public = (await client.get("/api/v1/fx/pairs")).json()
    assert {row["currency_code"] for row in public} == {"AAA", "BBB", "CCC", "EEE", "GGG"}

    # 已开仓 pair 的 identity 仍不可改（旧 `:195` 唯一限制删除后此保护不变）。
    aaa_id = next(row["id"] for row in public if row["currency_code"] == "AAA")
    assert (await client.patch(f"/api/v1/admin/fx/pairs/{aaa_id}",
                               json={"currency_code": "ZZZ"})).status_code == 409
    assert (await client.patch(f"/api/v1/admin/fx/pairs/{aaa_id}",
                               json={"currency_name": "Renamed"})).status_code == 409


@pytest.mark.asyncio
async def test_patch_to_trading_respects_limit_and_releases_on_pause(ctx):
    client, db, _ = ctx
    ids = {}
    for code in ("AAA", "BBB", "CCC"):
        ids[code] = (await client.post("/api/v1/admin/fx/pairs", json=_pair(code))).json()["id"]
    paused = await client.post("/api/v1/admin/fx/pairs", json=_pair("PPP", status="paused"))
    pid = paused.json()["id"]

    # 3 个 trading 时打开第 4 个 → 409
    assert (await client.patch(f"/api/v1/admin/fx/pairs/{pid}",
                               json={"status": "trading"})).status_code == 409
    assert await _trading_count(db) == 3

    # 暂停一个释放额度后可以打开
    assert (await client.patch(f"/api/v1/admin/fx/pairs/{ids['AAA']}",
                               json={"status": "paused"})).status_code == 200
    assert (await client.patch(f"/api/v1/admin/fx/pairs/{pid}",
                               json={"status": "trading"})).status_code == 200
    assert await _trading_count(db) == 3

    # 已在 trading 的 pair 自身 patch（不改 status）不受计数影响
    assert (await client.patch(f"/api/v1/admin/fx/pairs/{pid}",
                               json={"target_price": "1.5"})).status_code == 200


@pytest.mark.asyncio
async def test_concurrent_trading_creates_never_exceed_limit(ctx):
    client, db, _ = ctx
    responses = await asyncio.gather(*[
        client.post("/api/v1/admin/fx/pairs", json=_pair(f"C{index}"))
        for index in range(5)
    ])
    codes = sorted(response.status_code for response in responses)
    assert codes == [200, 200, 200, 409, 409], codes
    assert await _trading_count(db) == 3


@pytest.mark.asyncio
async def test_capacity_lock_uses_transactional_advisory_lock_on_postgres(ctx):
    """计数与写入的串行化：PG 走事务级 advisory lock，其他方言靠进程内锁。"""
    from app.api.v1 import admin_fx

    _, db, _ = ctx

    class FakeDB:
        def __init__(self, dialect_name: str):
            self._dialect_name = dialect_name
            self.statements: list[tuple[str, dict]] = []

        def get_bind(self):
            outer = self

            class _Bind:
                class dialect:  # noqa: N801 - 模拟 SQLAlchemy Dialect
                    name = outer._dialect_name

            return _Bind()

        async def execute(self, statement, params=None):
            self.statements.append((str(statement), params))

    pg = FakeDB("postgresql")
    await admin_fx._lock_trading_capacity(pg)
    assert len(pg.statements) == 1
    sql, params = pg.statements[0]
    assert "pg_advisory_xact_lock" in sql
    assert params == {"key": admin_fx._TRADING_CAPACITY_LOCK_KEY}

    lite = FakeDB("sqlite")
    await admin_fx._lock_trading_capacity(lite)
    assert lite.statements == []

    # 测试替身（无 bind 的 session 适配器）不触发 PG 分支也不会崩。
    assert admin_fx._dialect_name(db) == ""


@pytest.mark.asyncio
async def test_reduce_only_defaults_false_and_paused_stays_all_stopped(ctx):
    client, db, _ = ctx
    created = await client.post("/api/v1/admin/fx/pairs", json=_pair("RRR", status="paused"))
    pid = created.json()["id"]
    assert created.json()["reduce_only"] is False

    # status 变更绝不隐式翻转 reduce_only：paused 仍是全停旧语义。
    assert (await client.patch(f"/api/v1/admin/fx/pairs/{pid}",
                               json={"status": "trading"})).status_code == 200
    assert (await client.patch(f"/api/v1/admin/fx/pairs/{pid}",
                               json={"status": "paused"})).status_code == 200
    pair = (await db.execute(select(FxPair).where(FxPair.id == pid))).scalars().one()
    await db.refresh(pair)
    assert pair.reduce_only is False


@pytest.mark.asyncio
async def test_explicit_reduce_only_toggle_is_audited_and_public(ctx):
    client, db, _ = ctx
    created = await client.post("/api/v1/admin/fx/pairs", json=_pair("RRR", status="paused"))
    pid = created.json()["id"]

    toggled = await client.patch(f"/api/v1/admin/fx/pairs/{pid}", json={"reduce_only": True})
    assert toggled.status_code == 200, toggled.text
    assert toggled.json()["reduce_only"] is True

    audits = (await db.execute(select(AuditEvent).where(
        AuditEvent.ref_table == "fx_pair", AuditEvent.ref_id == pid,
        AuditEvent.event_type == "fx_pair_update",
    ))).scalars().all()
    toggle_rows = [row for row in audits if row.payload.get("action") == "reduce_only_toggle"]
    assert len(toggle_rows) == 1, [row.payload for row in audits]
    assert toggle_rows[0].payload["before"]["reduce_only"] == "False"
    assert toggle_rows[0].payload["after"]["reduce_only"] == "True"

    # 显式选择在 status 变更后保留（paused+reduce_only=true = 只许减仓）。
    assert (await client.patch(f"/api/v1/admin/fx/pairs/{pid}",
                               json={"status": "trading"})).status_code == 200
    pair = (await db.execute(select(FxPair).where(FxPair.id == pid))).scalars().one()
    await db.refresh(pair)
    assert pair.reduce_only is True

    # 公开响应暴露 reduce_only 状态位，但运营字段白名单不变。
    public = (await client.get("/api/v1/fx/pairs")).json()
    row = next(item for item in public if item["id"] == pid)
    assert row["reduce_only"] is True
    assert set(row) == PUBLIC_PAIR_FIELDS
    assert not (set(row) & OPERATOR_ONLY_FIELDS)

    # 管理端详情同样暴露 reduce_only，并保留 treasury 运营字段。
    detail = next(item for item in (await client.get("/api/v1/admin/fx/pairs")).json()
                  if item["id"] == pid)
    assert detail["reduce_only"] is True
    assert "gold_balance" in detail
