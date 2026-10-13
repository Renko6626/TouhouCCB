"""Read-only deployment guard requiring rebuild of legacy FX databases."""
import asyncio
from pathlib import Path
import sys
from sqlalchemy import inspect

REBUILD_MESSAGE = ('Legacy FX intervention schema detected; ordinary deployment is refused. '
    'Use scripts/rebuild_user_database.py with a separate empty target and follow '
    'docs/fx-database-rebuild.md before cutting over.')


class LegacyFxSchema(RuntimeError):
    pass


def require_rebuilt_schema(connection):
    metadata = inspect(connection)
    tables = set(metadata.get_table_names())
    if 'fx_event' in tables:
        raise LegacyFxSchema(REBUILD_MESSAGE)
    for table, removed in (
        ('fx_pair', {'target_price', 'target_min', 'target_max'}),
        ('fx_treasury', {'daily_spend', 'spend_date'}),
    ):
        if table in tables and removed.intersection(c['name'] for c in metadata.get_columns(table)):
            raise LegacyFxSchema(REBUILD_MESSAGE)


async def check_database():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.core.database import engine
    try:
        path = engine.url.database
        if engine.dialect.name == 'sqlite' and path and path != ':memory:' and not Path(path).exists():
            return
        async with engine.connect() as connection:
            await connection.run_sync(require_rebuilt_schema)
    finally:
        await engine.dispose()


if __name__ == '__main__':
    try:
        asyncio.run(check_database())
    except LegacyFxSchema as error:
        raise SystemExit(str(error)) from None
    except Exception:
        # Driver failures can contain connection URLs or credentials.
        raise SystemExit('FX rebuild preflight failed; check database connectivity and permissions.') from None
    print('safe')
