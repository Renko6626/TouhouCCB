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
    # FX 表（fx_pair/fx_treasury/fx_wallet/fx_trade/fx_event 及其后新增的
    # fx_short_position 等）由 fx_tables_20260928 及更晚的 revision 建；本测试 stamp
    # 在 ba3a85c3b675（FX 之前），before-state 不能预建，否则 upgrade head 撞
    # "table fx_pair already exists"。env.py 现在会 import app.models.fx，所以
    # SQLModel.metadata 里含这些表，必须按 fx_ 前缀整类剔除——只列固定表名会漏掉
    # 后续新增的表，而且 fx_short_position.pair_id → fx_pair.id 的外键会因 fx_pair
    # 缺席让 create_all 抛 NoReferencedTableError。
    # FX migration 自身的 upgrade/downgrade 由 test_fx_migration.py 专项验证。
    # 同理：liquidation_run/action 由 head revision credit_foundation_20260930 建，
    # 本测试 stamp 在它之前，before-state 不能预建（WP1 新增）。
    credit_table_names = {"liquidation_run", "liquidation_action"}
    for name, table in SQLModel.metadata.tables.items():
        if name.startswith("fx_"):
            continue
        table.to_metadata(before)

    def _strip_column(table, name: str) -> None:
        """从 before-state 的表副本里摘掉一列及其 FK / 索引。"""
        if name not in table.c:
            return
        column = table.c[name]
        for fk in list(column.foreign_keys):
            table.foreign_keys.discard(fk)
            column.foreign_keys.discard(fk)
        for index in list(table.indexes):
            if any(c.name == name for c in index.columns):
                table.indexes.discard(index)
        for constraint in list(table.constraints):
            if any(c.name == name for c in getattr(constraint, "columns", ())):
                table.constraints.discard(constraint)
        table._columns.remove(column)

    # 统一信贷（WP1）：预状态没有 credit 表，user/liquidation_events 也没有新列
    _strip_column(before.tables["user"], "economic_version")
    _strip_column(before.tables["user"], "credit_frozen")
    _strip_column(before.tables["liquidation_events"], "run_id")
    _strip_column(before.tables["liquidation_events"], "product")
    for name in credit_table_names:
        before.remove(before.tables[name])
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
