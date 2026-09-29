from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, create_engine, inspect, text
from sqlmodel import SQLModel


def test_fulfillment_migration_preserves_inventory_and_roundtrips(tmp_path, monkeypatch):
    """旧库存升级后仍可读；备注回填为空，回退/再次升级不会丢掉兑换码。"""
    import app.core.config as config_module

    path = tmp_path / "old_inventory.db"
    url = f"sqlite:///{path}"
    monkeypatch.setattr(config_module.settings, "DATABASE_URL", url)
    engine = create_engine(url)
    before = MetaData()
    for table in SQLModel.metadata.tables.values():
        table.to_metadata(before)
    codes = before.tables["redemption_code"]
    for name in ("redeemed_at", "redeemed_by_admin_id", "redemption_note"):
        column = codes.c[name]
        for constraint in list(codes.constraints):
            if any(item.name == name for item in constraint.columns):
                codes.constraints.discard(constraint)
        for index in list(codes.indexes):
            if any(item.name == name for item in index.columns):
                codes.indexes.discard(index)
        codes.foreign_keys.difference_update(column.foreign_keys)
        codes._columns.remove(column)
    try:
        before.create_all(engine)
        with engine.begin() as connection:
            connection.execute(codes.insert().values(batch_id=1, code_string="thfglegacy001", status="available"))
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.stamp(cfg, "ba3a85c3b675")
        command.upgrade(cfg, "head")
        with engine.connect() as connection:
            row = connection.execute(text("SELECT code_string, redeemed_at, redeemed_by_admin_id, redemption_note FROM redemption_code")).one()
            assert tuple(row) == ("thfglegacy001", None, None, "")
        assert any(fk["name"] == "fk_redemption_code_redeemed_by_admin_id_user" for fk in inspect(engine).get_foreign_keys("redemption_code"))
        command.downgrade(cfg, "ba3a85c3b675")
        assert "redeemed_at" not in {column["name"] for column in inspect(engine).get_columns("redemption_code")}
        command.upgrade(cfg, "head")
        with engine.connect() as connection:
            assert connection.execute(text("SELECT code_string, redemption_note FROM redemption_code")).one() == ("thfglegacy001", "")
    finally:
        engine.dispose()
