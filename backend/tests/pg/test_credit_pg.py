"""PostgreSQL 专用基座断言（WP1 计划）：

- 方言断言 + ``SELECT ... FOR UPDATE`` 冒烟（锁语义只有真 PG 才说明问题）
- 新列 server_default（老行升级后不需要回填）
- ``uq_liquidation_run_active_user`` 部分唯一索引在 PG 上真正生效
- 历史 credit foundation revision 在 PG 上往返后恢复其 schema 与约束
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

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


def _assert_credit_foundation_schema(conn):
    """Verify the historical revision's contract independently of later model additions."""
    inspector = inspect(conn)
    for table, columns in {
        "user": {"economic_version", "credit_frozen"},
        "fx_pair": {"reduce_only"},
        "liquidation_events": {"run_id", "product"},
        "liquidation_run": {
            "id", "user_id", "status", "trigger_source", "started_at", "updated_at",
            "next_round", "rounds", "last_group_product", "last_group_id",
            "last_blocked_reason", "closed_at", "pre_cash", "pre_debt",
            "pre_liquidation_equity", "total_proceeds", "total_repaid", "total_fee",
        },
        "liquidation_action": {
            "id", "run_id", "user_id", "round_no", "kind", "product", "group_id",
            "mode", "requested", "executed", "proceeds", "fee", "fee_currency",
            "repaid", "debt_after", "cash_after", "economic_version_after",
            "blocked_reason", "created_at",
        },
    }.items():
        actual = {c["name"] for c in inspector.get_columns(table)}
        if table in {"liquidation_run", "liquidation_action"}:
            assert columns == actual, table
        else:
            assert columns <= actual, table

    for table, names in {
        "user": {"economic_version", "credit_frozen"},
        "fx_pair": {"reduce_only"},
    }.items():
        columns = {c["name"]: c for c in inspector.get_columns(table)}
        for name in names:
            assert columns[name]["nullable"] is False
            assert columns[name]["default"] is not None

    active = next(i for i in inspector.get_indexes("liquidation_run")
                  if i["name"] == "uq_liquidation_run_active_user")
    assert active["unique"]
    assert active["column_names"] == ["user_id"]
    predicate = active["dialect_options"][f"{conn.dialect.name}_where"]
    assert "status" in str(predicate) and "'active'" in str(predicate)
    unique = inspector.get_unique_constraints("liquidation_action")
    assert any(c["column_names"] == ["run_id", "round_no"] for c in unique)
    for table, names in {
        "liquidation_run": {"ck_liquidation_run_status"},
        "liquidation_action": {"ck_liquidation_action_kind", "ck_liquidation_action_fee_currency"},
    }.items():
        assert names <= {c["name"] for c in inspector.get_check_constraints(table)}
    for table, column, target, ondelete in [
        ("liquidation_events", "run_id", "liquidation_run", "SET NULL"),
        ("liquidation_action", "run_id", "liquidation_run", "CASCADE"),
    ]:
        fk = next(f for f in inspector.get_foreign_keys(table)
                  if f["constrained_columns"] == [column])
        assert fk["referred_table"] == target
        assert fk["referred_columns"] == ["id"]
        assert fk["options"].get("ondelete") == ondelete


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


async def test_migration_roundtrip_on_pg_restores_credit_schema(pg_engine):
    """历史 revision 只负责自己的 schema；后续模型扩展不改变此契约。"""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    revision = _load_revision()

    def _run(sync_conn, direction: str) -> None:
        context = MigrationContext.configure(sync_conn)
        with Operations.context(context):
            getattr(revision, direction)()

    async with pg_engine.begin() as conn:
        await conn.execute(text(
            'INSERT INTO "user" (username, is_active, is_superuser, is_bot, cash, debt, '
            "economic_version, credit_frozen) VALUES ('pg_roundtrip', true, false, false, 12, 3, 4, true)"
        ))

    # fixture 创建当前 schema；往返只运行历史 credit foundation revision。
    async with pg_engine.begin() as conn:
        await conn.run_sync(lambda c: _run(c, "downgrade"))
    async with pg_engine.begin() as conn:
        await conn.run_sync(lambda c: _run(c, "upgrade"))
    async with pg_engine.connect() as conn:
        await conn.run_sync(_assert_credit_foundation_schema)
        row = (await conn.execute(text(
            'SELECT cash, debt, economic_version, credit_frozen FROM "user" '
            "WHERE username='pg_roundtrip'"
        ))).one()
        assert tuple(row) == (Decimal("12"), Decimal("3"), 0, False)
