"""PostgreSQL 专用基座断言（WP1 计划）：

- 方言断言 + ``SELECT ... FOR UPDATE`` 冒烟（锁语义只有真 PG 才说明问题）
- 新列 server_default（老行升级后不需要回填）
- ``uq_liquidation_run_active_user`` 部分唯一索引在 PG 上真正生效
- 新 revision 在 PG 上 upgrade/downgrade 往返后与 metadata 零差异
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel

from app.models.base import User
from app.models.credit import LiquidationRun
from app.models.fx import FxPair

pytestmark = [pytest.mark.pg, pytest.mark.asyncio]


def _load_revision():
    path = next(Path(__file__).parents[2].glob("alembic/versions/*credit_foundation_*.py"))
    spec = importlib.util.spec_from_file_location("credit_foundation_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_dialect_and_select_user_for_update_smoke(pg_engine, pg_sessionmaker):
    """基座自检：方言是 postgresql，且 FOR UPDATE 锁路径可用。"""
    assert pg_engine.dialect.name == "postgresql"
    async with pg_sessionmaker() as s:
        async with s.begin():
            s.add(User(username="pg_smoke", casdoor_id="pg_smoke", cash=Decimal("10")))
    async with pg_sessionmaker() as s:
        async with s.begin():
            u = (
                await s.execute(select(User).where(User.username == "pg_smoke").with_for_update())
            ).scalars().one()
            assert u.cash == Decimal("10")
            assert u.economic_version == 0
            assert u.credit_frozen is False


async def test_credit_columns_have_server_defaults(pg_sessionmaker):
    """raw INSERT 不写新列也能落库：server_default 是 additive 迁移的前提。"""
    async with pg_sessionmaker() as s:
        async with s.begin():
            await s.execute(
                text(
                    'INSERT INTO "user" (username, is_active, is_superuser, is_bot, cash, debt) '
                    "VALUES ('pg_defaults', true, false, false, 0, 0)"
                )
            )
            await s.execute(
                text(
                    "INSERT INTO fx_pair (currency_code, currency_name, status, gold_reserve,"
                    " foreign_reserve, target_price, initial_price, target_min, target_max,"
                    " buy_fee_rate, sell_fee_rate, pool_version, created_at, updated_at) "
                    "VALUES ('PGX', 'PGX', 'draft', 1, 1, 1, 1, 0.5, 2, 0, 0, 1, now(), now())"
                )
            )
        row = (
            await s.execute(
                text('SELECT economic_version, credit_frozen FROM "user" WHERE username=\'pg_defaults\'')
            )
        ).one()
        assert tuple(row) == (0, False)
        assert (
            await s.execute(text("SELECT reduce_only FROM fx_pair WHERE currency_code='PGX'"))
        ).scalar_one() is False


async def test_partial_unique_index_allows_one_active_run_per_user(pg_sessionmaker):
    """F12：每用户最多一个 active run；终态 run 可以有多条。"""
    async with pg_sessionmaker() as s:
        async with s.begin():
            u = User(username="pg_run", cash=Decimal("0"), debt=Decimal("0"))
            s.add(u)
            await s.flush()
            uid = u.id

    def _run(status: str) -> LiquidationRun:
        return LiquidationRun(
            user_id=uid, status=status, trigger_source="scheduler",
            started_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
        )

    async with pg_sessionmaker() as s:
        async with s.begin():
            s.add(_run("active"))

    # 第二个 active 必须被部分唯一索引拒绝
    async with pg_sessionmaker() as s:
        s.add(_run("active"))
        with pytest.raises(IntegrityError):
            await s.flush()
        await s.rollback()

    # 把唯一 active 收尾后，才能再开一个新 run；终态记录可以并存
    async with pg_sessionmaker() as s:
        async with s.begin():
            current = (await s.execute(
                select(LiquidationRun).where(
                    LiquidationRun.user_id == uid, LiquidationRun.status == "active"
                )
            )).scalars().one()
            current.status = "recovered"
            s.add(current)
            s.add(_run("blocked"))
    async with pg_sessionmaker() as s:
        async with s.begin():
            s.add(_run("active"))
    async with pg_sessionmaker() as s:
        runs = (await s.execute(select(LiquidationRun).where(LiquidationRun.user_id == uid))).scalars().all()
        # 失败的第二个 active 没有落库；终态 recovered + blocked + 新 active = 3
        assert sorted(r.status for r in runs) == ["active", "blocked", "recovered"]


async def test_migration_roundtrip_on_pg_matches_metadata(pg_engine):
    """新 revision 在 PG 上 downgrade → upgrade 往返后与 SQLModel.metadata 零差异。"""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    revision = _load_revision()

    def _run(sync_conn, direction: str) -> None:
        context = MigrationContext.configure(sync_conn)
        with Operations.context(context):
            getattr(revision, direction)()

    def _diff(sync_conn):
        return compare_metadata(MigrationContext.configure(sync_conn), SQLModel.metadata)

    # pg_engine fixture 已 create_all 出完整当前 schema，等价于 upgrade 后的状态。
    async with pg_engine.begin() as conn:
        await conn.run_sync(lambda c: _run(c, "downgrade"))
    async with pg_engine.begin() as conn:
        await conn.run_sync(lambda c: _run(c, "upgrade"))
    async with pg_engine.connect() as conn:
        diff = await conn.run_sync(_diff)
    assert diff == [], f"PG 迁移往返后仍有 metadata 差异: {diff}"
