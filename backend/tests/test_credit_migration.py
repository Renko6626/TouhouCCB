"""Actual baseline upgrade, model compatibility, revision boundary and safe rollback."""
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlmodel import SQLModel

from app.core.schema import BASELINE_REVISION, classify_schema, require_schema_current
from app.models.base import User
from app.models.title import Title
from app.models.credit import LiquidationRun


def migrate(connection, revision="head", *, downgrade=False):
    cfg = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    cfg.attributes["connection"] = connection
    (command.downgrade if downgrade else command.upgrade)(cfg, revision)


def assert_schema(connection):
    assert classify_schema(connection) == "upgradeable"
    assert compare_metadata(MigrationContext.configure(connection), SQLModel.metadata) == []
    inspector = inspect(connection)
    assert set(inspector.get_table_names()) == set(SQLModel.metadata.tables) | {"alembic_version"}
    # compare_metadata omits check-constraint comparison, so verify these explicitly.
    for name, model in SQLModel.metadata.tables.items():
        expected = {c.name for c in model.constraints if c.__class__.__name__ == "CheckConstraint"}
        assert expected == {c['name'] for c in inspector.get_check_constraints(name)}
    fk = next(f for f in inspector.get_foreign_keys("market") if f['constrained_columns'] == ['winning_outcome_id'])
    assert fk['referred_table'] == 'outcome'
    fk = next(f for f in inspector.get_foreign_keys("user") if f['constrained_columns'] == ['equipped_title_id'])
    assert fk['options']['ondelete'] == 'SET NULL'


@pytest.fixture
def baseline(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/baseline.db")
    @event.listens_for(engine, "connect")
    def enforce_fks(dbapi, record):
        dbapi.execute("PRAGMA foreign_keys=ON")
    with engine.begin() as connection:
        assert classify_schema(connection) == "empty"
        migrate(connection)
    yield engine
    engine.dispose()


def test_baseline_schema_and_empty_roundtrip(baseline):
    with baseline.begin() as connection:
        assert_schema(connection)
        migrate(connection, "base", downgrade=True)
        assert inspect(connection).get_table_names() == ['alembic_version']
        migrate(connection)
        assert_schema(connection)


def exercise_model_constraints(engine):
    with Session(engine) as session:
        title = Title(name="preserved title")
        user = User(username="preserved", cash=12, debt=3)
        session.add_all([title, user]); session.commit()
        user.equipped_title_id = title.id
        session.commit()
        uid = user.id
        assert session.execute(select(User)).scalar_one().cash == 12
        session.add(LiquidationRun(user_id=uid, status="active", trigger_source="scheduler"))
        session.commit()
        session.add(LiquidationRun(user_id=uid, status="active", trigger_source="scheduler"))
        with pytest.raises(IntegrityError): session.commit()
        session.rollback()
        session.add(LiquidationRun(user_id=uid, status="invalid", trigger_source="scheduler"))
        with pytest.raises(IntegrityError): session.commit()
        session.rollback()
        session.add(LiquidationRun(user_id=uid + 999999, status="active", trigger_source="scheduler"))
        with pytest.raises(IntegrityError): session.commit()
        session.rollback()
        session.delete(title);session.commit()
        session.refresh(user)
        assert user.equipped_title_id is None


def test_models_constraints_and_populated_downgrade_retains_all_tables(baseline):
    exercise_model_constraints(baseline)
    with baseline.begin() as connection:
        tables = set(inspect(connection).get_table_names())
        with pytest.raises(RuntimeError, match="populated"):
            migrate(connection, "base", downgrade=True)
        assert set(inspect(connection).get_table_names()) == tables
        assert connection.execute(text('SELECT cash, debt FROM "user"')).one() == (12, 3)
        assert connection.execute(text('SELECT version_num FROM alembic_version')).scalar_one() == BASELINE_REVISION


def test_sqlite_reset_watermark_prevents_empty_business_id_reuse(baseline):
    with baseline.begin() as connection:
        connection.execute(text("INSERT INTO sqlite_sequence(name,seq) VALUES ('market', 500)"))
        from app.models.base import Market
        with Session(connection) as session:
            market = Market(title="new market")
            session.add(market);session.flush()
            assert market.id == 501


@pytest.mark.parametrize("state", ['old', 'unknown', 'partial', 'unversioned'])
def test_preflight_rejects_unsupported_schema_without_changes(baseline, state):
    with baseline.begin() as connection:
        if state in ('old', 'unknown'):
            connection.execute(text('UPDATE alembic_version SET version_num=:v'), {'v': 'fx_pool_events_20261013' if state == 'old' else 'head'})
        elif state == 'partial':
            connection.execute(text('DROP TABLE "transaction"'))
        else:
            connection.execute(text('DROP TABLE alembic_version'))
        tables = set(inspect(connection).get_table_names())
        with pytest.raises(RuntimeError): classify_schema(connection)
        assert set(inspect(connection).get_table_names()) == tables


@pytest.mark.asyncio
async def test_startup_requires_installed_head_and_does_not_initialize(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/startup.db")
    try:
        with pytest.raises(RuntimeError, match="Empty schema"):
            await require_schema_current(engine)
        async with engine.begin() as connection:
            assert await connection.run_sync(lambda c: inspect(c).get_table_names()) == []
            await connection.run_sync(migrate)
        await require_schema_current(engine)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_future_incremental_upgrade_preserves_data_and_startup_boundary(tmp_path, monkeypatch):
    """A later real revision remains deploy-upgradeable; startup waits for its head."""
    import shutil
    from alembic.script import ScriptDirectory
    from sqlalchemy.ext.asyncio import create_async_engine
    from app.core import schema

    scripts_path = tmp_path / 'alembic'
    shutil.copytree(Path(__file__).parents[1] / 'alembic', scripts_path)
    (scripts_path / 'versions' / 'future.py').write_text(
        "from alembic import op\nimport sqlalchemy as sa\n"
        "revision = 'test_future_increment'\n"
        f"down_revision = '{BASELINE_REVISION}'\n"
        "def upgrade():\n    op.create_table('future_feature', sa.Column('id', sa.Integer(), primary_key=True))\n"
        "def downgrade():\n    op.drop_table('future_feature')\n"
    )
    cfg = Config(str(Path(__file__).parents[1] / 'alembic.ini'))
    cfg.set_main_option('script_location', str(scripts_path))
    monkeypatch.setattr(schema, '_scripts', lambda: ScriptDirectory.from_config(cfg))
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/increment.db")
    try:
        async with engine.begin() as connection:
            def install(c):
                cfg.attributes['connection'] = c
                command.upgrade(cfg, BASELINE_REVISION)
                c.execute(User.__table__.insert().values(username='retained', cash=42))
            await connection.run_sync(install)
            assert await connection.run_sync(classify_schema) == 'upgradeable'
        with pytest.raises(RuntimeError, match='upgrade required'):
            await require_schema_current(engine)
        async with engine.begin() as connection:
            def advance(c):
                cfg.attributes['connection'] = c
                command.upgrade(cfg, 'head')
                assert 'future_feature' in inspect(c).get_table_names()
                assert c.execute(select(User.cash).where(User.username == 'retained')).scalar_one() == 42
            await connection.run_sync(advance)
        await require_schema_current(engine)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_init_db_refuses_existing_schema_before_demo_or_data_changes(tmp_path, monkeypatch):
    import init_db
    from sqlalchemy.ext.asyncio import create_async_engine
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/bootstrap.db")
    monkeypatch.setattr(init_db, 'engine', engine)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(migrate)
            await connection.execute(User.__table__.insert().values(username='keep', cash=19))
        with pytest.raises(RuntimeError, match='empty database'):
            await init_db.init_db()
        async with engine.connect() as connection:
            assert (await connection.execute(select(User.cash))).scalar_one() == 19
            assert (await connection.execute(text('SELECT count(*) FROM market'))).scalar_one() == 0
    finally:
        await engine.dispose()
