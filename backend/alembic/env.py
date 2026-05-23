"""Alembic env — 接 SQLModel.metadata + 从 app.core.config 读 DATABASE_URL。

约定：
- 用 settings.build_db_url() 拿当前环境的 DB URL（与运行时一致）。
- alembic 自身是同步驱动，所以把 asyncpg/aiosqlite/asyncmy 替成 sync 对应驱动。
- target_metadata = SQLModel.metadata，所有 model 模块在 import 时注册进去。
"""
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

# 把 backend/ 加入 sys.path，让 `from app...` import 工作
BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

# 导入所有 model 模块，确保 SQLModel.metadata 包含所有表
import app.models.base  # noqa: F401, E402
import app.models.redemption  # noqa: F401, E402
import app.models.title  # noqa: F401, E402
from app.core.config import settings  # noqa: E402

config = context.config

# 用 settings 覆盖 sqlalchemy.url（alembic 是 sync，所以替成 sync 驱动）
db_url = settings.build_db_url()
db_url = db_url.replace("postgresql+asyncpg", "postgresql+psycopg2")
db_url = db_url.replace("sqlite+aiosqlite", "sqlite")
db_url = db_url.replace("mysql+asyncmy", "mysql+pymysql")
config.set_main_option("sqlalchemy.url", db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    """Offline 模式：直接拿 URL 生成 SQL，不连 DB。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Online 模式：建 engine、连 DB、跑 migration。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        # sqlite 不支持 ALTER constraint，必须开 render_as_batch 让 alembic 走
        # copy-and-move 策略。pg/mysql 走原生 ALTER，保持现状。
        is_sqlite = connection.dialect.name == "sqlite"
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=is_sqlite,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
