from contextlib import asynccontextmanager
from typing import AsyncGenerator, AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlmodel import SQLModel

from app.core.config import settings


DATABASE_URL = settings.build_db_url()
IS_SQLITE = DATABASE_URL.startswith("sqlite+aiosqlite://")

engine_kwargs = {
    "echo": settings.DB_ECHO,
}

if IS_SQLITE:
    engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    engine_kwargs.update(
        {
            "pool_pre_ping": True,
            "pool_recycle": settings.DB_POOL_RECYCLE,
            "pool_size": settings.DB_POOL_SIZE,
            "max_overflow": settings.DB_MAX_OVERFLOW,
        }
    )

engine = create_async_engine(DATABASE_URL, **engine_kwargs)

# 异步 Session 工厂
async_session_maker = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession,
)

# 初始化数据库（创建表）
async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

# FastAPI 依赖：获取异步数据库会话
async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session


@asynccontextmanager
async def managed_transaction(session: AsyncSession) -> AsyncIterator[None]:
    """Provide one safe write boundary for request-scoped AsyncSession."""
    if session.in_transaction():
        try:
            yield
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    else:
        async with session.begin():
            yield
