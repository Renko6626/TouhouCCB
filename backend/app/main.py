import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine, init_db
from app.core.admin import setup_admin
from app.api.v1 import auth, user, market, chart, stream, loan, site_config as site_config_api
from app.models import redemption as _redemption_models  # noqa: F401  确保 SQLModel.metadata 注册兑换码三张表
from app.services.loan_sweep import start_scheduler, stop_scheduler
from app.services.loan_migrate import auto_migrate

from dotenv import load_dotenv

load_dotenv()

access_logger = logging.getLogger("thccb.access")

# 跳过高频或长连接路径，避免淹没日志：
# - /health 容器探活 10s/次
# - /api/v1/stream/* SSE，单连接可能持续一小时
# - /docs /redoc /openapi.json 仅开发调试用
_LOG_SKIP_PREFIXES = (
    "/health",
    "/api/v1/stream",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/favicon.ico",
    # SQLAdmin 自身的静态资源（/api/v1/admin/statics/*）和 UI 渲染请求噪声大，
    # 业务性动作（创建/结算/调整现金等）都走 /api/v1/market/* 和 /api/v1/user/*/adjust-cash
    # 等业务端点，会被正常记录，不依赖 admin 路径。
    "/api/v1/admin",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup: 建表 → LoanV1 幂等补列/种默认配置 → 挂载 admin → 启动 loan sweep
    await init_db()
    await auto_migrate()
    setup_admin(app, engine)
    await start_scheduler()
    yield
    # shutdown: 停 sweep + 释放连接池，避免优雅停机时残留连接
    await stop_scheduler()
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


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """简单请求日志：method / path / status / elapsed_ms。

    跳过高频探活（/health）与长连接（SSE），避免淹没日志；
    5xx 用 warning 级别提升告警敏感度。
    """
    path = request.url.path
    if any(path.startswith(p) for p in _LOG_SKIP_PREFIXES):
        return await call_next(request)

    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = (time.perf_counter() - start) * 1000
        access_logger.exception(
            "%s %s EXC %.1fms", request.method, path, elapsed_ms,
        )
        raise

    elapsed_ms = (time.perf_counter() - start) * 1000
    level = logging.WARNING if response.status_code >= 500 else logging.INFO
    access_logger.log(
        level,
        "%s %s %d %.1fms",
        request.method,
        path,
        response.status_code,
        elapsed_ms,
    )
    return response

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
app.include_router(loan.router, prefix="/api/v1/loan", tags=["Loan"])
app.include_router(site_config_api.router, prefix="/api/v1/admin", tags=["Admin"])

from app.api.v1 import redemption as redemption_api, admin_redemption as admin_redemption_api
app.include_router(redemption_api.router, prefix="/api/v1/redemption", tags=["Redemption"])
app.include_router(admin_redemption_api.router, prefix="/api/v1/admin/redemption", tags=["AdminRedemption"])


@app.get("/")
async def root():
    return {"message": "欢迎来到大天狗交易所", "docs": "/docs"}


@app.get("/health", tags=["Meta"], summary="健康检查（含 DB ping）")
async def health():
    """返回 200 + db ok 表示进程与数据库都正常；DB 不通时返回 503。

    响应带 db_latency_ms（SELECT 1 往返耗时，含建连+查询）便于运维
    观测数据库趋势慢化。可用于容器 healthcheck、nginx upstream 探活、
    外部监控。
    """
    start = time.perf_counter()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"db unavailable: {type(exc).__name__}")
    db_latency_ms = round((time.perf_counter() - start) * 1000, 2)
    return {"status": "ok", "db": "ok", "db_latency_ms": db_latency_ms}
