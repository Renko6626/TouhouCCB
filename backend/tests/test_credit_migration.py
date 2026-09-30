"""WP1：新 revision 的 schema 往返 + init_db(create_all) 与 metadata 零差异。

两层证据：
1. `SQLModel.metadata.create_all`（init_db.py 路径）与 metadata 零差异；
2. 新 revision 在完整当前 schema 上 downgrade → upgrade 后与 metadata 零差异，
   且既有业务行不丢（additive 迁移）。

PG 侧同一往返由 tests/pg/test_credit_pg.py 覆盖（需要 TEST_PG_DATABASE_URL）。
"""
import importlib.util
import os
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select, text
from sqlmodel import SQLModel

# 触发 metadata 注册（与 alembic/env.py 一致）
import app.models.base  # noqa: F401,E402
import app.models.redemption  # noqa: F401,E402
import app.models.title  # noqa: F401,E402
import app.models.ledger  # noqa: F401,E402
import app.models.audit  # noqa: F401,E402
import app.models.bot  # noqa: F401,E402
import app.models.fx  # noqa: F401,E402
import app.models.credit  # noqa: F401,E402

BACKEND_DIR = Path(__file__).parents[1]


def _load_revision():
    path = next(BACKEND_DIR.glob("alembic/versions/*credit_foundation_*.py"))
    spec = importlib.util.spec_from_file_location("credit_foundation_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_revision(conn, direction: str) -> None:
    context = MigrationContext.configure(conn)
    with Operations.context(context):
        getattr(_load_revision(), direction)()


def _diff(conn):
    return compare_metadata(MigrationContext.configure(conn), SQLModel.metadata)


def test_init_db_create_all_has_no_metadata_diff(tmp_path):
    """init_db.py 建出的 schema 必须与 metadata 一致（否则 alembic 会重复加列）。"""
    engine = create_engine(f"sqlite:///{tmp_path}/init.db")
    SQLModel.metadata.create_all(engine)
    with engine.connect() as conn:
        assert _diff(conn) == []


def test_credit_schema_objects_exist(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/schema.db")
    SQLModel.metadata.create_all(engine)
    inspector = inspect(engine)
    names = set(inspector.get_table_names())
    assert {"liquidation_run", "liquidation_action"} <= names

    for table, columns in {
        "user": {"economic_version", "credit_frozen"},
        "fx_pair": {"reduce_only"},
        "liquidation_events": {"run_id", "product"},
    }.items():
        assert columns <= {c["name"] for c in inspector.get_columns(table)}, table

    # 每用户最多一个 active run：部分唯一索引必须带上 WHERE 子句
    idx = {i["name"]: i for i in inspector.get_indexes("liquidation_run")}
    assert idx["uq_liquidation_run_active_user"]["unique"] == 1
    with engine.connect() as conn:
        ddl = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE name='uq_liquidation_run_active_user'")
        ).scalar_one()
    assert "WHERE status = 'active'" in ddl

    # LiquidationEvent.run_id FK 的 ondelete 语义（run 删除后保留公示行）
    fks = {fk["constrained_columns"][0]: fk for fk in inspector.get_foreign_keys("liquidation_events")}
    assert fks["run_id"]["referred_table"] == "liquidation_run"
    assert fks["run_id"]["options"].get("ondelete") == "SET NULL"


def test_migration_roundtrip_preserves_rows_and_matches_metadata(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/roundtrip.db")
    SQLModel.metadata.create_all(engine)   # 等价于 revision upgrade 后的状态
    with engine.begin() as conn:
        conn.execute(text(
            'INSERT INTO "user" (username, is_active, is_superuser, is_bot, cash, debt,'
            " economic_version, credit_frozen) VALUES ('keep', 1, 0, 0, 12, 3, 4, 1)"
        ))
        conn.execute(text(
            "INSERT INTO liquidation_run (user_id, status, trigger_source, started_at, updated_at,"
            " next_round, rounds, pre_cash, pre_debt, pre_liquidation_equity, total_proceeds,"
            " total_repaid, total_fee) SELECT id, 'active', 'scheduler', CURRENT_TIMESTAMP,"
            " CURRENT_TIMESTAMP, 1, 0, 0, 0, 0, 0, 0, 0 FROM \"user\" WHERE username='keep'"
        ))
        conn.execute(text(
            "INSERT INTO liquidation_events (user_id, triggered_at, pre_cash, pre_debt,"
            " pre_holdings_value, pre_net_worth, sold_positions_count, total_proceeds, repaid_amount,"
            " remaining_debt, post_cash, trigger_source, mode, run_id, product)"
            " SELECT id, CURRENT_TIMESTAMP, 0, 0, 0, 0, 0, 0, 0, 0, 0, 'scheduler', 'emergency',"
            " (SELECT max(id) FROM liquidation_run), 'lmsr' FROM \"user\" WHERE username='keep'"
        ))
        conn.execute(text(
            "INSERT INTO fx_pair (currency_code, currency_name, status, gold_reserve, foreign_reserve,"
            " target_price, initial_price, target_min, target_max, buy_fee_rate, sell_fee_rate,"
            " pool_version, reduce_only, created_at, updated_at)"
            " VALUES ('KEEP', 'KEEP', 'paused', 1, 1, 1, 1, 0.5, 2, 0, 0, 1, 0,"
            " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))

    with engine.begin() as conn:
        _run_revision(conn, "downgrade")
    with engine.connect() as conn:
        names = set(inspect(conn).get_table_names())
        assert not ({"liquidation_run", "liquidation_action"} & names)
        assert {"run_id", "product"} & {c["name"] for c in inspect(conn).get_columns("liquidation_events")} == set()
        # 既有资金/身份行不受 downgrade 影响
        row = conn.execute(text('SELECT username, cash, debt FROM "user" WHERE username=\'keep\'')).one()
        assert tuple(row) == ("keep", Decimal("12"), Decimal("3"))
        assert conn.execute(text("SELECT count(*) FROM liquidation_events")).scalar_one() == 1
        assert conn.execute(text("SELECT status FROM fx_pair WHERE currency_code='KEEP'")).scalar_one() == "paused"

    with engine.begin() as conn:
        _run_revision(conn, "upgrade")
    with engine.connect() as conn:
        assert _diff(conn) == []
        # downgrade 会删掉新列，其旧值随列一起丢（默认回 0/false）——资金行不受影响
        assert conn.execute(
            text('SELECT economic_version FROM "user" WHERE username=\'keep\'')
        ).scalar_one() == 0
        assert conn.execute(
            text('SELECT cash, debt FROM "user" WHERE username=\'keep\'')
        ).one() == (Decimal("12"), Decimal("3"))
        assert conn.execute(text("SELECT count(*) FROM liquidation_events")).scalar_one() == 1
        # downgrade 丢的是 run/action 运行记录（资金不受影响）
        assert conn.execute(text("SELECT count(*) FROM liquidation_run")).scalar_one() == 0


def test_downgrade_upgrade_is_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/idem.db")
    SQLModel.metadata.create_all(engine)
    for _ in range(2):
        with engine.begin() as conn:
            _run_revision(conn, "downgrade")
        with engine.begin() as conn:
            _run_revision(conn, "upgrade")
        with engine.connect() as conn:
            assert _diff(conn) == []
