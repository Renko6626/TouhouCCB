import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect, select


def _load_revision():
    path = next(Path(__file__).parents[1].glob("alembic/versions/*_add_fx_tables.py"))
    spec = importlib.util.spec_from_file_location("fx_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fx_migration_upgrade_and_downgrade_preserves_existing_rows():
    revision = _load_revision()
    engine = create_engine("sqlite://")
    metadata = MetaData()
    user = Table("user", metadata, Column("id", Integer, primary_key=True))
    unrelated = Table("unrelated", metadata, Column("id", Integer, primary_key=True), Column("value", String))
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(user.insert().values(id=1))
        conn.execute(unrelated.insert().values(id=1, value="keep"))
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            revision.upgrade()
        names = set(inspect(conn).get_table_names())
        assert {"fx_pair", "fx_treasury", "fx_wallet", "fx_trade", "fx_event"} <= names
        assert conn.execute(select(unrelated.c.value)).scalar_one() == "keep"
        with Operations.context(context):
            revision.downgrade()
        names = set(inspect(conn).get_table_names())
        assert not ({"fx_pair", "fx_treasury", "fx_wallet", "fx_trade", "fx_event"} & names)
        assert conn.execute(select(unrelated.c.value)).scalar_one() == "keep"
