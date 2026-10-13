"""Refuse legacy schemas without changing them."""
import pytest
from sqlalchemy import create_engine, inspect, text
from scripts.check_fx_rebuild_deploy import require_rebuilt_schema

@pytest.mark.parametrize('ddl', [
    'CREATE TABLE fx_event (id INTEGER PRIMARY KEY)',
    'CREATE TABLE fx_pair (id INTEGER PRIMARY KEY, target_price NUMERIC)',
    'CREATE TABLE fx_treasury (id INTEGER PRIMARY KEY, daily_spend NUMERIC)',
])
def test_legacy_schema_is_refused_without_changes(ddl):
    engine = create_engine('sqlite://')
    with engine.begin() as connection:
        connection.execute(text(ddl))
        before = inspect(connection).get_table_names()
        with pytest.raises(RuntimeError, match='separate empty target'):
            require_rebuilt_schema(connection)
        assert inspect(connection).get_table_names() == before
    engine.dispose()

def test_clean_or_rebuilt_schema_passes():
    engine = create_engine('sqlite://')
    with engine.begin() as connection:
        require_rebuilt_schema(connection)
        connection.execute(text('CREATE TABLE fx_pair (id INTEGER PRIMARY KEY, initial_price NUMERIC)'))
        connection.execute(text('CREATE TABLE fx_treasury (id INTEGER PRIMARY KEY, foreign_balance NUMERIC)'))
        require_rebuilt_schema(connection)
    engine.dispose()
