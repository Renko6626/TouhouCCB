import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine, init_db
from app.core.admin import setup_admin
from app.api.v1 import auth, user, market, chart, stream, loan, site_config as site_config_api
from app.models import redemption as _redemption_models  # noqa: F401  确保 SQLModel.metadata 注册兑换码三张表
from app.services.loan_sweep import (
    start_scheduler as start_loan_scheduler,
    stop_scheduler as stop_loan_scheduler,
)
from app.services.liquidation_sweep import (
    start_scheduler as start_liquidation_scheduler,
    stop_scheduler as stop_liquidation_scheduler,
)
from app.services.bot_detection import (
    start_scheduler as start_bot_detection_scheduler,
    stop_scheduler as stop_bot_detection_scheduler,
)
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


def _set_no_store_for_api(path: str, response):
    if path.startswith("/api/v1/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup: 建表 → LoanV1 幂等补列/种默认配置 → 挂载 admin → 启动 loan/liquidation sweep
    await init_db()
    await auto_migrate()
    setup_admin(app, engine)
    # ── candle 表 race-window 兜底扫（spec § 6.3）──
    # 覆盖 migration→新代码上线之间可能漏的 buy/sell。
    try:
        await _resync_recent_candles()
    except Exception as e:
        # 兜底失败不能阻塞启动；记日志后续手工跑 backfill CLI
        logging.getLogger("thccb.candle").exception("resync_recent_candles failed: %s", e)
    await start_loan_scheduler()
    await start_liquidation_scheduler()
    await start_bot_detection_scheduler()
    yield
    # shutdown: 停 sweep + 释放连接池，避免优雅停机时残留连接
    # 启动顺序 loan → liquidation → bot_detection，停止时反序
    await stop_bot_detection_scheduler()
    await stop_liquidation_scheduler()
    await stop_loan_scheduler()
    await engine.dispose()


async def _resync_recent_candles(window_hours: int = 1) -> None:
    """启动时清重建近 window_hours 内的 candle 桶，覆盖 migration→新代码上线之间的
    race window。

    幂等性：先 DELETE 涉及窗口的 candle 行（按最大 interval=1h 对齐到桶边界、再
    向前推一个 max_interval 以包住跨边界桶），再从 Transaction 表完整重建。这样
    多次重启都得到同样结果，绝不 double-count volume/n_trades。

    与 upsert_candles 的累加语义配合：DELETE 之后桶不存在 → 第一次 UPSERT 走 INSERT
    分支，后续同 bucket 多笔走 UPDATE 累加，最终值 = 从 0 开始重新积累。
    """
    from app.core.database import async_session_maker
    from app.models.base import Outcome, OutcomeCandle, Transaction, TransactionType
    from app.services.candle_writer import (
        CANDLE_INTERVALS,
        compute_candle_rows,
        upsert_candles,
    )
    from sqlalchemy import delete, select

    # 安全 cutoff：在 (now - window) 前再推一个 max_interval（1h），并 floor 到
    # max_interval 边界。这保证：
    #   1. 任何被回放的 trade 落入的桶都在 DELETE 范围内（跨 cutoff 边界的 1h 桶不漏删）
    #   2. cutoff 之前的桶不被 DELETE 也不被回放 → 保持原状
    max_interval_sec = max(step for _, step in CANDLE_INTERVALS)
    raw_cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    safe_epoch = int(raw_cutoff.timestamp()) - max_interval_sec
    safe_epoch -= safe_epoch % max_interval_sec
    cutoff = datetime.fromtimestamp(safe_epoch, tz=timezone.utc)

    async with async_session_maker() as s:
        market_ids_result = (await s.execute(
            select(Outcome.market_id)
            .join(Transaction, Transaction.outcome_id == Outcome.id)
            .where(
                Transaction.timestamp >= cutoff,
                Transaction.type.in_([TransactionType.BUY, TransactionType.SELL]),
            )
            .distinct()
        )).all()
        market_ids = [r[0] for r in market_ids_result]

    if not market_ids:
        return

    for mid in market_ids:
        async with async_session_maker() as s:
            outcomes = (await s.execute(
                select(Outcome).where(Outcome.market_id == mid).order_by(Outcome.id)
            )).scalars().all()
            outcome_ids = [o.id for o in outcomes]
            if not outcome_ids:
                continue

            # 关键：先清掉窗口内已有 candle 行，避免 UPSERT 累加路径污染
            await s.execute(
                delete(OutcomeCandle).where(
                    OutcomeCandle.outcome_id.in_(outcome_ids),
                    OutcomeCandle.bucket_start >= cutoff,
                )
            )

            txs = (await s.execute(
                select(Transaction)
                .where(
                    Transaction.outcome_id.in_(outcome_ids),
                    Transaction.timestamp >= cutoff,
                    Transaction.type.in_([TransactionType.BUY, TransactionType.SELL]),
                )
                .order_by(Transaction.timestamp.asc())
            )).scalars().all()

            for tx in txs:
                if tx.market_prices_post and len(tx.market_prices_post) == len(outcome_ids):
                    new_prices = [float(p) for p in tx.market_prices_post]
                else:
                    new_prices = [
                        float(tx.post_market_price) if oid == tx.outcome_id
                        else 1.0 / len(outcome_ids) for oid in outcome_ids
                    ]
                rows = compute_candle_rows(
                    traded_outcome_id=tx.outcome_id,
                    outcome_ids=outcome_ids,
                    new_prices=new_prices,
                    traded_shares=tx.shares,
                    ts=tx.timestamp,
                )
                await upsert_candles(s, rows)
            await s.commit()


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
        return _set_no_store_for_api(path, await call_next(request))

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
    return _set_no_store_for_api(path, response)

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

from app.api.v1 import danmuku as danmuku_api
app.include_router(danmuku_api.router, prefix="/api/v1/danmuku", tags=["Danmuku"])

from app.api.v1 import admin_stats as admin_stats_api
app.include_router(admin_stats_api.router, prefix="/api/v1/admin/stats", tags=["AdminStats"])

from app.api.v1 import admin_liquidation as admin_liquidation_api
app.include_router(admin_liquidation_api.router, prefix="/api/v1/admin/liquidation", tags=["AdminLiquidation"])


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
