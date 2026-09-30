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


def test_archive_migration_defaults_existing_pairs_and_preserves_rows():
    path = next(Path(__file__).parents[1].glob('alembic/versions/*-fx_pair_archive.py'))
    spec = importlib.util.spec_from_file_location('fx_archive_migration', path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    engine = create_engine('sqlite://')
    metadata = MetaData()
    pair = Table('fx_pair', metadata, Column('id', Integer, primary_key=True), Column('currency_code', String))
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(pair.insert().values(id=1, currency_code='USD'))
        with Operations.context(MigrationContext.configure(conn)):
            revision.upgrade()
        reflected = Table('fx_pair', MetaData(), autoload_with=conn)
        assert conn.execute(select(reflected.c.archived)).scalar_one() is False
        conn.execute(reflected.insert().values(id=2, currency_code='EUR'))
        assert conn.execute(select(reflected.c.archived).where(reflected.c.id == 2)).scalar_one() is False
        with Operations.context(MigrationContext.configure(conn)):
            revision.downgrade()
        assert 'archived' not in {c['name'] for c in inspect(conn).get_columns('fx_pair')}
        assert conn.execute(select(pair.c.currency_code).order_by(pair.c.id)).scalars().all() == ['USD', 'EUR']
