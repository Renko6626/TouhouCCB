"""PostgreSQL 专用测试基座（WP1 计划：`tests/pg/` 独立库、每测试 drop_all/create_all）。

跑法（默认 `pytest.ini` 的 `-m "not pg"` 会跳过本目录）：

    TEST_PG_DATABASE_URL=postgresql+asyncpg://user:pw@127.0.0.1:5432/thccb_test \\
        python -m pytest -q -m pg tests/pg/

设计：
- 只连 ``TEST_PG_DATABASE_URL`` 指定的**一次性测试库**；库名必须含 "test"，
  否则直接 UsageError 拒绝跑（防手抖指向生产库）。
- 每个测试前 ``drop_all`` + ``create_all``：PG 上没有 SQLite 的 fsync 成本。
- 父级 ``tests/conftest.py`` 的 autouse ``setup_db`` 会去动 SQLite 引擎，这里用
  同名 no-op fixture 覆盖掉，避免两套库互相干扰。
"""
from __future__ import annotations

import os

import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel

# 确保 metadata 里有全部表（父 conftest 也会 import app.main，这里显式声明依赖）
import app.models.base  # noqa: F401
import app.models.redemption  # noqa: F401
import app.models.title  # noqa: F401
import app.models.ledger  # noqa: F401
import app.models.audit  # noqa: F401
import app.models.bot  # noqa: F401
import app.models.fx  # noqa: F401
import app.models.credit  # noqa: F401

PG_URL_ENV = "TEST_PG_DATABASE_URL"


@pytest.fixture
def setup_db():
    """覆盖父 conftest 的 SQLite drop_all/create_all：PG 测试自带隔离库。"""
    yield


# 注意：这里**不能**用 pytest_collection_modifyitems 做全局 skip —— conftest 的钩子是
# 进程级注册的，会把整套测试（不只 tests/pg/）都标记为 skip。缺 TEST_PG_DATABASE_URL
# 时由 pg_engine fixture 自行 pytest.skip。


def _checked_url() -> str:
    url = os.environ.get(PG_URL_ENV, "").strip()
    if not url:
        pytest.skip(f"{PG_URL_ENV} 未设置")
    if not url.startswith("postgresql"):
        pytest.skip(f"{PG_URL_ENV} 不是 postgresql URL")
    parsed = make_url(url)
    name = (parsed.database or "").lower()
    if "test" not in name:
        raise pytest.UsageError(
            f"{PG_URL_ENV} 的库名 {parsed.database!r} 不像一次性测试库（必须含 'test'）——拒绝 drop_all"
        )
    return url


@pytest_asyncio.fixture
async def pg_engine():
    """每测试独立 async engine + 干净 schema；方言断言 postgresql。"""
    url = _checked_url()
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            dialect = conn.dialect.name
    except Exception as exc:  # 连不上就 skip，不让整套测试红
        await engine.dispose()
        pytest.skip(f"PostgreSQL 测试库不可达: {type(exc).__name__}: {exc}")
    assert dialect == "postgresql", f"pg fixture 只跑 PostgreSQL，实际 {dialect}"

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
        await conn.run_sync(SQLModel.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def pg_sessionmaker(pg_engine):
    return async_sessionmaker(pg_engine, expire_on_commit=False)
