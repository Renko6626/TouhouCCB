from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scripts.check_fx_short_rollout import SHORT_REVISION, check_database, require_safe_rollout


def scripts():
    return ScriptDirectory.from_config(Config(str(Path(__file__).parents[1] / "alembic.ini")))


def test_routine_deploy_preserves_enabled_short_opening():
    # Do not pin the latest migration: later upgrades remain routine deployments.
    directory = scripts()
    assert require_safe_rollout(tuple(directory.get_heads()), directory, True)


def test_first_short_rollout_rejects_opening_already_enabled():
    directory = scripts()
    predecessor = directory.get_revision(SHORT_REVISION).down_revision
    with pytest.raises(RuntimeError, match="refusing initial rollout"):
        require_safe_rollout((predecessor,), directory, True)


def test_empty_database_can_bootstrap_without_enabling_short_opening():
    assert not require_safe_rollout((), scripts(), False)


def test_initial_rollout_rechecks_gate_after_migration():
    directory = scripts()
    with pytest.raises(RuntimeError, match="refusing initial rollout"):
        require_safe_rollout(tuple(directory.get_heads()), directory, True, require_closed=True)


@pytest.mark.asyncio
async def test_missing_sqlite_preflight_does_not_create_bootstrap_database(tmp_path, monkeypatch):
    from app.core import database
    path = tmp_path / "new.db"
    monkeypatch.setattr(database, "engine", create_async_engine(f"sqlite+aiosqlite:///{path}"))
    assert not await check_database(False)
    assert not path.exists()


@pytest.mark.asyncio
async def test_preflight_reads_real_config_table_before_initial_rollout(tmp_path, monkeypatch):
    from app.core import database
    from app.models.base import SiteConfig
    test_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'configured.db'}")
    async with test_engine.begin() as connection:
        await connection.run_sync(SiteConfig.__table__.create)
        await connection.execute(SiteConfig.__table__.insert().values(
            key="fx_short_enabled", value="true", value_type="bool"))
        await connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
        predecessor = scripts().get_revision(SHORT_REVISION).down_revision
        await connection.execute(text("INSERT INTO alembic_version VALUES (:revision)"), {"revision": predecessor})
    monkeypatch.setattr(database, "engine", test_engine)
    monkeypatch.setattr(database, "async_session_maker", async_sessionmaker(test_engine))
    with pytest.raises(RuntimeError, match="refusing initial rollout"):
        await check_database(False)
