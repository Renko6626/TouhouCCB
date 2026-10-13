"""Prevent the production adapter from operating on a non-compose database."""
import pytest
from scripts import production_fx_rebuild as production


def test_compose_source_keeps_credentials_without_logging(monkeypatch):
    monkeypatch.setattr(production.settings, 'DATABASE_URL', 'postgresql+asyncpg://thccb:private@postgres:5432/thccb')
    url = production.fixed_source_url()
    assert url.password == 'private'
    assert url.set(database='thccb_rebuild_123').database == 'thccb_rebuild_123'
    assert url.database == 'thccb'


@pytest.mark.parametrize('url', [
    'postgresql://thccb:private@external:5432/thccb',
    'postgresql://thccb:private@postgres:5433/thccb',
    'postgresql://thccb:private@postgres:5432/other',
    'postgresql://other:private@postgres:5432/thccb',
    'sqlite:////app/data/thccb.db',
    'postgresql://thccb:private@postgres:5432/thccb?host=external',
])
def test_rejects_database_that_backup_and_cutover_would_not_cover(monkeypatch, url):
    monkeypatch.setattr(production.settings, 'DATABASE_URL', url)
    with pytest.raises(ValueError, match='fixed compose PostgreSQL source'):
        production.fixed_source_url()
