"""Read-only preflight distinguishing first short rollout from routine updates."""
import argparse
import asyncio
from pathlib import Path
import sys

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

SHORT_REVISION = "fx_short_debt_20260930"


def require_safe_rollout(
    heads: tuple[str, ...], scripts: ScriptDirectory, opening_enabled: bool,
    *, require_closed: bool = False,
) -> bool:
    installed = any(revision.revision == SHORT_REVISION
                    for revision in scripts.iterate_revisions(heads, "base"))
    if opening_enabled and (require_closed or not installed):
        raise RuntimeError("FX short opening gate is already enabled; refusing initial rollout")
    return installed


async def check_database(require_closed: bool) -> bool:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from app.core.database import async_session_maker, engine
    from app.models.base import SiteConfig
    from app.services.site_config import get_bool_or

    directory = ScriptDirectory.from_config(Config(str(root / "alembic.ini")))
    try:
        # Connecting to a missing SQLite file creates it. Keep it absent so the
        # deploy script still chooses init_db bootstrap rather than ALTERs.
        db_path = engine.url.database
        if engine.dialect.name == "sqlite" and db_path and db_path != ":memory:" and not Path(db_path).exists():
            return require_safe_rollout((), directory, False, require_closed=require_closed)
        async with engine.connect() as connection:
            heads, has_config = await connection.run_sync(lambda conn: (
                MigrationContext.configure(conn).get_current_heads(),
                inspect(conn).has_table(SiteConfig.__tablename__),
            ))
        enabled = False
        if has_config:
            async with async_session_maker() as session:
                enabled = await get_bool_or(session, "fx_short_enabled", False)
        return require_safe_rollout(heads, directory, enabled, require_closed=require_closed)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-closed", action="store_true",
                        help="Recheck the closed gate during an initial rollout")
    args = parser.parse_args()
    try:
        installed = asyncio.run(check_database(args.require_closed))
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
    print("existing" if installed else "initial")
