from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine, init_db
from app.core.admin import setup_admin
from app.api.v1 import auth, user, market, chart, stream

from dotenv import load_dotenv

load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup: 建表 + 挂载 admin
    await init_db()
    setup_admin(app, engine)
    yield
    # shutdown: 释放连接池，避免优雅停机时残留连接
    await engine.dispose()


app = FastAPI(title="东方炒炒币 (Touhou Exchange)", lifespan=lifespan)

# CORS — 从配置读取允许的源
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

# 注册认证模块
# 最终路径示例：
# 注册：POST /api/v1/auth/register
# 登录：POST /api/v1/auth/jwt/login
# 查看自己：GET /api/v1/auth/me
app.include_router(auth.router, prefix="/api/v1/auth", tags=["Auth"])
app.include_router(user.router, prefix="/api/v1/user", tags=["UserAssets"])
app.include_router(market.router, prefix="/api/v1/market", tags=["Market"])
app.include_router(chart.router, prefix="/api/v1/chart", tags=["Chart"])
app.include_router(stream.router, prefix="/api/v1/stream", tags=["Stream"])


@app.get("/")
async def root():
    return {"message": "欢迎来到大天狗交易所", "docs": "/docs"}


@app.get("/health", tags=["Meta"], summary="健康检查（含 DB ping）")
async def health():
    """返回 200 + db ok 表示进程与数据库都正常；DB 不通时返回 503。

    可用于容器 healthcheck、nginx upstream 探活、外部监控。
    """
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"db unavailable: {type(exc).__name__}")
    return {"status": "ok", "db": "ok"}
