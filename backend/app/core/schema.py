"""Read-only schema boundary shared by application startup and deploy preflight."""
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.script.revision import ResolutionError
from alembic.util.exc import CommandError
from sqlalchemy import inspect, text

BASELINE_REVISION = "business_baseline_20261013"


def _scripts():
    return ScriptDirectory.from_config(Config(str(Path(__file__).parents[2] / "alembic.ini")))


def _installed_revision(connection):
    rows = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    if len(rows) != 1:
        raise RuntimeError("Missing or ambiguous schema revision; use business reset")
    return rows[0]


def classify_schema(connection) -> str:
    """Accept an empty target or a complete baseline descendant; never mutate it."""
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    if not tables:
        if inspector.get_view_names():
            raise RuntimeError("Partial database schema; use business reset")
        return "empty"
    if "alembic_version" not in tables:
        raise RuntimeError("Unversioned database schema; use business reset")
    revision = _installed_revision(connection)
    scripts = _scripts()
    try:
        known = scripts.get_revision(revision)
        if known is None or known.revision != revision:
            raise RuntimeError("Old or unknown schema revision; use business reset")
        ancestry = {r.revision for r in scripts.iterate_revisions(revision, "base")}
    except (CommandError, ResolutionError, KeyError) as exc:
        raise RuntimeError("Old or unknown schema revision; use business reset") from exc
    if BASELINE_REVISION not in ancestry:
        raise RuntimeError("Schema predates business baseline; use business reset")
    baseline = scripts.get_revision(BASELINE_REVISION).module
    for name, columns in baseline.SCHEMA_TABLE_COLUMNS.items():
        required = set(columns)
        # This descendant deliberately removes the baseline cash-lock column.
        # Older revisions still require it; other baseline columns remain mandatory.
        if name == "fx_short_position" and "remove_short_cash_lock_20261014" in ancestry:
            required.remove("restricted_gold")
        if name not in tables or not required <= {c["name"] for c in inspector.get_columns(name)}:
            raise RuntimeError("Partial business database schema; use business reset")
    return "upgradeable"


async def require_schema_current(engine) -> None:
    """All app instances require installed head before taking ownership or seeding."""
    async with engine.connect() as connection:
        def check(sync_connection):
            if classify_schema(sync_connection) == "empty":
                raise RuntimeError("Empty schema; run alembic upgrade head before startup")
            if _installed_revision(sync_connection) != _scripts().get_current_head():
                raise RuntimeError("Schema upgrade required before startup")
        await connection.run_sync(check)


async def _preflight():
    from app.core.database import engine
    try:
        async with engine.connect() as connection:
            print(await connection.run_sync(classify_schema))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    import asyncio
    asyncio.run(_preflight())
