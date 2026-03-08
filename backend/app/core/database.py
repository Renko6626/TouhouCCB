from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlmodel import SQLModel

from app.core.config import settings


DATABASE_URL = settings.build_db_url()

# MySQL 异步引擎（推荐 asyncmy；若你用 aiomysql 也一样）
engine = create_async_engine(
    DATABASE_URL,
    echo=settings.DB_ECHO,          # 建议从配置读，而不是写死 True
    pool_pre_ping=True,             # ✅ MySQL 常见断连：每次取连接先 ping
    pool_recycle=settings.DB_POOL_RECYCLE,  # ✅ 防止 MySQL wait_timeout 导致连接失效
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    # isolation_level="READ COMMITTED",     # 可选：你想更贴近交易系统可显式设
)

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
