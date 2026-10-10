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


def _short_revision():
    path = next(Path(__file__).parents[1].glob('alembic/versions/*_fx_short_debt.py'))
    spec = importlib.util.spec_from_file_location('short_revision', path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    return revision


def test_borrow_buy_migration_preserves_history_and_refuses_lossy_downgrade():
    import pytest
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    path = next(Path(__file__).parents[1].glob('alembic/versions/*_fx_borrow_buy.py'))
    spec = importlib.util.spec_from_file_location('borrow_buy_revision', path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    with create_engine('sqlite://').begin() as conn:
        _legacy_short_schema(conn)
        with Operations.context(MigrationContext.configure(conn)):
            _short_revision().upgrade()
            revision.upgrade()
        assert conn.execute(text('SELECT purpose,input_amount,borrow_amount FROM fx_trade')).one() == ('spot', 1, None)
        for principal in ('NULL', '-1', '2'):
            with pytest.raises(IntegrityError):
                conn.execute(text(f"UPDATE fx_trade SET purpose='borrow_buy',borrow_amount={principal}"))
        conn.execute(text("UPDATE fx_trade SET purpose='borrow_buy',borrow_amount=0.5"))
        with Operations.context(MigrationContext.configure(conn)):
            with pytest.raises(RuntimeError, match='history'):
                revision.downgrade()
        assert conn.execute(text('SELECT cash,debt FROM user')).one() == (123, 45)
        # Restore the legacy fixture identity to exercise a reversible downgrade.
        conn.execute(text("UPDATE fx_trade SET purpose='spot',borrow_amount=NULL"))
        with Operations.context(MigrationContext.configure(conn)):
            revision.downgrade()
        assert 'borrow_amount' not in {c['name'] for c in inspect(conn).get_columns('fx_trade')}
        assert conn.execute(text('SELECT input_amount,output_amount FROM fx_trade')).one() == (1, 2)


def _legacy_short_schema(conn):
    """Use real legacy DDL, retaining an independent cash authority."""
    from sqlalchemy import text
    conn.execute(text('CREATE TABLE user (id INTEGER PRIMARY KEY, cash NUMERIC(16,6), debt NUMERIC(16,6))'))
    with Operations.context(MigrationContext.configure(conn)):
        _load_revision().upgrade()
        path = next(Path(__file__).parents[1].glob('alembic/versions/*credit_foundation*'))
        spec = importlib.util.spec_from_file_location('credit_revision', path)
        credit = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(credit)
        # Only credit-owned tables/columns needed for this upgrade; legacy event DDL below.
        conn.execute(text('CREATE TABLE liquidation_events (id INTEGER PRIMARY KEY, pre_net_worth NUMERIC(16,6) NOT NULL, pre_holdings_value NUMERIC(16,6) NOT NULL)'))
        credit.upgrade()
    conn.execute(text('INSERT INTO user(id,cash,debt,economic_version,credit_frozen) VALUES(1,123,45,0,0)'))
    conn.execute(text("INSERT INTO fx_pair(id,currency_code,currency_name,status,gold_reserve,foreign_reserve,target_price,initial_price,target_min,target_max,buy_fee_rate,sell_fee_rate,pool_version,created_at,updated_at) VALUES(1,'USD','Dollar','trading',100,200,1,1,0.5,2,0,0,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
    conn.execute(text('INSERT INTO fx_wallet(id,user_id,pair_id,foreign_amount,cost_basis,updated_at) VALUES(1,1,1,7,8,CURRENT_TIMESTAMP)'))
    conn.execute(text("INSERT INTO fx_trade(id,pair_id,user_id,side,input_amount,output_amount,min_out,fee_amount,pre_gold_reserve,pre_foreign_reserve,post_gold_reserve,post_foreign_reserve,post_price,source,created_at) VALUES(1,1,1,'buy',1,2,0,0,100,200,101,198,0.51,'player',CURRENT_TIMESTAMP)"))
    conn.execute(text('INSERT INTO liquidation_events(id,pre_net_worth,pre_holdings_value) VALUES(1,12,34)'))


def test_short_upgrade_preserves_money_history_constraints_and_safe_downgrade():
    import pytest
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    revision = _short_revision()
    with create_engine('sqlite://').begin() as conn:
        _legacy_short_schema(conn)
        with Operations.context(MigrationContext.configure(conn)):
            revision.upgrade()
        assert conn.execute(text('SELECT cash,debt FROM user')).one() == (123,45)
        assert conn.execute(text('SELECT foreign_amount,cost_basis FROM fx_wallet')).one() == (7,8)
        assert conn.execute(text('SELECT purpose,input_amount,output_amount FROM fx_trade')).one() == ('spot',1,2)
        assert conn.execute(text('SELECT pre_net_worth,pre_holdings_value FROM liquidation_events')).one() == (12,34)
        conn.execute(text('UPDATE liquidation_events SET pre_net_worth=NULL,pre_holdings_value=NULL'))
        for amounts, at in [('1,0,0,0','NULL'), ('-1,0,0,0','CURRENT_TIMESTAMP'), ('0,0,1,0','NULL')]:
            with pytest.raises(IntegrityError):
                conn.execute(text(f'INSERT INTO fx_short_position(user_id,pair_id,principal_foreign,interest_foreign,restricted_gold,proceeds_basis_gold,interest_last_accrued_at,created_at,updated_at) VALUES(1,1,{amounts},{at},CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)'))
        with pytest.raises(IntegrityError):
            conn.execute(text('UPDATE fx_wallet SET foreign_amount=-1'))
        conn.execute(text('INSERT INTO fx_short_position(user_id,pair_id,principal_foreign,interest_foreign,restricted_gold,proceeds_basis_gold,interest_last_accrued_at,created_at,updated_at) VALUES(1,1,1,0,1,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)'))
        with Operations.context(MigrationContext.configure(conn)):
            with pytest.raises(RuntimeError, match='debt|obligation'):
                revision.downgrade()
        conn.execute(text('UPDATE fx_short_position SET principal_foreign=0,restricted_gold=0,proceeds_basis_gold=0,interest_last_accrued_at=NULL'))
        conn.execute(text('UPDATE liquidation_events SET pre_net_worth=12,pre_holdings_value=34'))
        with Operations.context(MigrationContext.configure(conn)):
            revision.downgrade()
        assert 'fx_short_position' not in inspect(conn).get_table_names()
        assert conn.execute(text('SELECT cash,debt FROM user')).one() == (123,45)
        assert conn.execute(text('SELECT input_amount,output_amount FROM fx_trade')).one() == (1,2)


def test_short_upgrade_rejects_negative_legacy_wallet_without_conversion():
    import pytest
    from sqlalchemy import text
    revision = _short_revision()
    with create_engine('sqlite://').begin() as conn:
        _legacy_short_schema(conn)
        conn.execute(text('UPDATE fx_wallet SET foreign_amount=-1'))
        with Operations.context(MigrationContext.configure(conn)):
            with pytest.raises(RuntimeError, match='wallet'):
                revision.upgrade()
        assert 'fx_short_position' not in inspect(conn).get_table_names()
        assert conn.execute(text('SELECT foreign_amount FROM fx_wallet')).scalar_one() == -1


def _candle_revision():
    path = next(Path(__file__).parents[1].glob('alembic/versions/*fx_candle_storage.py'))
    spec = importlib.util.spec_from_file_location('fx_candle_revision', path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    return revision


_FX_PAIR_INSERT = (
    "INSERT INTO fx_pair(id,currency_code,currency_name,status,gold_reserve,foreign_reserve,"
    "target_price,initial_price,target_min,target_max,buy_fee_rate,sell_fee_rate,pool_version,"
    "created_at,updated_at) VALUES(1,'USD','Dollar','trading',100,200,1,1,0.5,2,0,0,1,"
    "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
)
_FX_TRADE_INSERT = (
    "INSERT INTO fx_trade(id,pair_id,user_id,side,input_amount,output_amount,min_out,fee_amount,"
    "pre_gold_reserve,pre_foreign_reserve,post_gold_reserve,post_foreign_reserve,post_price,source,"
    "created_at) VALUES(1,1,1,'buy',1,2,0,0,100,200,101,198,0.51,'player',CURRENT_TIMESTAMP)"
)


def _candle_insert(interval='1m', high=1, low=1, n=1):
    return (
        "INSERT INTO fx_candle(pair_id,interval,bucket_start,open_price,high_price,low_price,"
        "close_price,gold_volume,n_trades,first_trade_at,first_trade_id,last_trade_at,"
        "last_trade_id,updated_at) VALUES(1,'%s','2026-10-01 00:00:00',1,%s,%s,1,1,%s,"
        "'2026-10-01 00:00:00',1,'2026-10-01 00:00:00',1,'2026-10-01 00:00:00')"
        % (interval, high, low, n)
    )


def test_candle_migration_adds_derived_only_and_preserves_ledger():
    """The candle migration must add derived structures without touching money data."""
    import pytest
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    revision = _candle_revision()
    engine = create_engine('sqlite://')
    metadata = MetaData()
    user = Table('user', metadata, Column('id', Integer, primary_key=True))
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(user.insert().values(id=1))
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            _load_revision().upgrade()          # real FX ledger tables
        conn.execute(text(_FX_PAIR_INSERT))
        conn.execute(text(_FX_TRADE_INSERT))
        with Operations.context(context):
            revision.upgrade()

        names = set(inspect(conn).get_table_names())
        assert {'fx_candle', 'fx_market_data_state'} <= names
        assert conn.execute(text('SELECT currency_code FROM fx_pair')).scalar_one() == 'USD'
        assert conn.execute(text('SELECT input_amount,output_amount FROM fx_trade')).one() == (1, 2)
        assert 'ix_fx_trade_pair_id_id' in {i['name'] for i in inspect(conn).get_indexes('fx_trade')}

        for bad in (
            _candle_insert(n=-1),
            _candle_insert(high=0.5, low=1),
            _candle_insert(interval='2m'),
        ):
            with pytest.raises(IntegrityError):
                conn.execute(text(bad))
        with pytest.raises(IntegrityError):
            conn.execute(text(
                "INSERT INTO fx_market_data_state(pair_id,last_trade_id,history_version,"
                "history_ready,updated_at) VALUES(1,-1,'v',0,CURRENT_TIMESTAMP)"
            ))

        with Operations.context(context):
            revision.downgrade()

        names = set(inspect(conn).get_table_names())
        assert not ({'fx_candle', 'fx_market_data_state'} & names)
        assert 'ix_fx_trade_pair_id_id' not in {i['name'] for i in inspect(conn).get_indexes('fx_trade')}
        assert conn.execute(text('SELECT currency_code FROM fx_pair')).scalar_one() == 'USD'
        assert conn.execute(text('SELECT input_amount FROM fx_trade')).scalar_one() == 1


def test_intervention_cleanup_preserves_accounts_pool_and_lending_balances():
    """Removing obsolete metadata must retain live money and restore usable DDL."""
    from sqlalchemy import text
    path = next(Path(__file__).parents[1].glob('alembic/versions/*fx_remove_intervention.py'))
    spec = importlib.util.spec_from_file_location('cleanup_revision', path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    with create_engine('sqlite://').begin() as conn:
        conn.execute(text('CREATE TABLE user (id INTEGER PRIMARY KEY, cash NUMERIC(16,6), debt NUMERIC(16,6))'))
        from app.models.base import SiteConfig
        SiteConfig.__table__.create(conn)
        conn.execute(text('INSERT INTO user VALUES(1,123,45)'))
        with Operations.context(MigrationContext.configure(conn)):
            _load_revision().upgrade()
        conn.execute(text(_FX_PAIR_INSERT))
        conn.execute(text('INSERT INTO fx_treasury(pair_id,gold_balance,foreign_balance,daily_spend,updated_at) VALUES(1,17,29,3,CURRENT_TIMESTAMP)'))
        conn.execute(text("INSERT INTO siteconfig(key,value,value_type,updated_at) VALUES('fx_enabled','false','bool',CURRENT_TIMESTAMP),('fx_daily_budget','10','decimal',CURRENT_TIMESTAMP)"))
        with Operations.context(MigrationContext.configure(conn)):
            revision.upgrade()
        assert 'fx_event' not in inspect(conn).get_table_names()
        assert {'target_price','target_min','target_max'}.isdisjoint(c['name'] for c in inspect(conn).get_columns('fx_pair'))
        assert {'daily_spend','spend_date'}.isdisjoint(c['name'] for c in inspect(conn).get_columns('fx_treasury'))
        assert conn.execute(text('SELECT cash,debt FROM user')).one() == (123,45)
        assert conn.execute(text('SELECT gold_reserve,foreign_reserve,initial_price FROM fx_pair')).one() == (100,200,1)
        assert conn.execute(text('SELECT gold_balance,foreign_balance FROM fx_treasury')).one() == (17,29)
        assert conn.execute(text('SELECT key,value FROM siteconfig')).all() == [('fx_enabled','false')]
        with Operations.context(MigrationContext.configure(conn)):
            revision.downgrade()
        assert 'fx_event' in inspect(conn).get_table_names()
        assert conn.execute(text('SELECT target_price,target_min,target_max FROM fx_pair')).one() == (1,0.5,2)
        assert conn.execute(text('SELECT gold_balance,foreign_balance,daily_spend FROM fx_treasury')).one() == (17,29,0)
        conn.execute(text("INSERT INTO fx_event(pair_id,title,kind) VALUES(1,'restored structure','macro')"))
        assert conn.execute(text('SELECT cash,debt FROM user')).one() == (123,45)


def test_cleanup_roundtrip_schema_matches_current_model_metadata():
    """Empty-target metadata bootstrap and cleanup migration produce the same FX schema."""
    from sqlmodel import SQLModel
    from app.models.fx import FxPair, FxTreasury
    from app.models.base import SiteConfig, User
    from app.models.title import Title
    path = next(Path(__file__).parents[1].glob('alembic/versions/*fx_remove_intervention.py'))
    spec = importlib.util.spec_from_file_location('cleanup_roundtrip', path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    with create_engine('sqlite://').begin() as conn:
        SQLModel.metadata.create_all(conn, tables=[Title.__table__, User.__table__, SiteConfig.__table__, FxPair.__table__, FxTreasury.__table__])
        def shape(table):
            inspector = inspect(conn)
            return (
                {(c['name'], str(c['type']), c['nullable']) for c in inspector.get_columns(table)},
                {(c['name'], c['sqltext']) for c in inspector.get_check_constraints(table)},
                {(c['name'], tuple(c['column_names'])) for c in inspector.get_unique_constraints(table)},
                {(c['name'], tuple(c['column_names'])) for c in inspector.get_indexes(table)},
                {(tuple(c['constrained_columns']), c['referred_table'], tuple(c['referred_columns'])) for c in inspector.get_foreign_keys(table)},
            )
        expected = {table:shape(table) for table in ('fx_pair','fx_treasury')}
        with Operations.context(MigrationContext.configure(conn)):
            revision.downgrade()
            revision.upgrade()
        assert {table:shape(table) for table in expected} == expected
        assert 'fx_event' not in inspect(conn).get_table_names()
