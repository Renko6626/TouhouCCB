"""验证 alembic upgrade/downgrade 在 sqlite 上能跑通且幂等。

这个测试模拟 prod 升级路径：
1) 起一个"上一版本 (679d34cb5986) 之后、还没跑新迁移"的 schema
   （即包含所有现有表，但没有 title_* 5 张表，user 表没有 equipped_title_id 列）
2) 跑 alembic stamp 679d34cb5986
3) 跑 alembic upgrade head → 新表 + 新列出现
4) 跑 alembic downgrade -1 → 新表 + 新列消失
5) 再跑 alembic upgrade head → 幂等通过

# 实现注意：
- conftest 已经把 SQLModel.metadata 通过 app.main 链路加载完整（含 title 5 张表
  和 user.equipped_title_id），所以要构造 before-state 需要临时摘除这些。
- alembic env.py 用 `settings.build_db_url()` 覆盖 cfg.url，所以仅 set_main_option
  不够；必须 rebind `app.core.config.settings` 让 env.py 下次 reload 时拿到
  指向 tempfile 的新 URL。
- 测试结束 finally 块负责把 metadata 和 settings 全部恢复，否则后续测试看到的
  User 表会缺 equipped_title_id、engine 指向错的 DB。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from alembic.config import Config
from alembic import command
from sqlalchemy import create_engine, inspect


def _make_alembic_cfg(db_url: str) -> Config:
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def test_upgrade_downgrade_roundtrip():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    db_url = f"sqlite:///{db_path}"
    sync_engine = create_engine(db_url)

    from sqlmodel import SQLModel
    import app.models.base  # noqa: F401
    import app.models.redemption  # noqa: F401
    import app.core.config as _cfg

    user_table = SQLModel.metadata.tables['user']
    title_table_names = [
        "title", "title_code_batch", "title_code",
        "user_title", "market_required_title",
    ]

    # 1) 摘 equipped_title_id 列和它的 FK 约束（finally 恢复）
    removed_constraints = []
    for c in list(user_table.constraints):
        cols = getattr(c, 'columns', None)
        if cols is not None and any(col.name == 'equipped_title_id' for col in cols):
            user_table.constraints.discard(c)
            removed_constraints.append(c)

    removed_col = None
    removed_fks = []
    if 'equipped_title_id' in user_table.c:
        removed_col = user_table.c.equipped_title_id
        for fk in list(removed_col.foreign_keys):
            removed_fks.append(fk)
            removed_col.foreign_keys.discard(fk)
            user_table.foreign_keys.discard(fk)
        user_table._columns.remove(removed_col)

    # outcome.initial_shares 由 c4d5e6f7a8b9 加，before-state 同样要剔掉（finally 恢复）
    outcome_table = SQLModel.metadata.tables["outcome"]
    removed_outcome_col = None
    if 'initial_shares' in outcome_table.c:
        removed_outcome_col = outcome_table.c.initial_shares
        outcome_table._columns.remove(removed_outcome_col)

    # 白名单 keep_tables（剔除 title 相关 5 张表 + ledger_entry + audit_event）
    # ledger_entry / audit_event 由其后续 migration（b2cd21122925 / a7c3e9d1f402）建，
    # before-state 不应预先 create_all，否则 upgrade head 会 "table already exists"。
    keep_tables = [
        t for name, t in SQLModel.metadata.tables.items()
        if name not in title_table_names and name not in ("ledger_entry", "audit_event")
    ]

    # 2) 保存原 settings，临时 rebind 到 tempfile DB URL
    original_settings = _cfg.settings
    original_db_url_env = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = db_url
    _cfg.settings = type(_cfg.settings)()

    try:
        # 构造 before-state schema（不含 title* 表，user 不含 equipped_title_id）
        SQLModel.metadata.create_all(sync_engine, tables=keep_tables)

        cfg = _make_alembic_cfg(db_url)
        command.stamp(cfg, "679d34cb5986")

        # 3) upgrade head → 新表 + 新列出现
        command.upgrade(cfg, "head")
        insp = inspect(sync_engine)
        names = set(insp.get_table_names())
        for t in title_table_names:
            assert t in names, f"{t} not created"
        user_cols = {c["name"] for c in insp.get_columns("user")}
        assert "equipped_title_id" in user_cols
        assert "initial_shares" in {c["name"] for c in insp.get_columns("outcome")}

        # 4) downgrade 到 title migration 之前的 baseline → title 表 + 列消失
        # （head 现在是 ledger migration b2cd21122925，downgrade 到 679d34cb5986 会
        #  依次回退 ledger + title 两个 migration；不能再用 "-1"，那只回退 ledger。）
        command.downgrade(cfg, "679d34cb5986")
        insp = inspect(sync_engine)
        names = set(insp.get_table_names())
        for t in title_table_names:
            assert t not in names, f"{t} not dropped"
        user_cols = {c["name"] for c in insp.get_columns("user")}
        assert "equipped_title_id" not in user_cols
        assert "initial_shares" not in {c["name"] for c in insp.get_columns("outcome")}

        # 5) 幂等再 upgrade head
        command.upgrade(cfg, "head")
        insp = inspect(sync_engine)
        names = set(insp.get_table_names())
        assert "title" in names
        user_cols = {c["name"] for c in insp.get_columns("user")}
        assert "equipped_title_id" in user_cols
    finally:
        sync_engine.dispose()
        try:
            os.unlink(db_path)
        except OSError:
            pass

        # 恢复 settings + DATABASE_URL
        _cfg.settings = original_settings
        if original_db_url_env is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original_db_url_env

        # 恢复 metadata：把列和 FK 加回去
        if removed_outcome_col is not None:
            outcome_table.append_column(removed_outcome_col)
        if removed_col is not None:
            user_table.append_column(removed_col)
            for fk in removed_fks:
                removed_col.foreign_keys.add(fk)
                user_table.foreign_keys.add(fk)
        for c in removed_constraints:
            user_table.constraints.add(c)
