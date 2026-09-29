import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.core.config import settings
from app.core.database import async_session_maker, engine, init_db
from app.core.admin import setup_admin
from app.api.v1 import auth, user, market, chart, stream, loan, site_config as site_config_api
from app.api.v1 import fx as fx_api
from app.api.v1 import admin_fx as admin_fx_api
from app.api.v1 import fx_stream as fx_stream_api
from app.models import redemption as _redemption_models  # noqa: F401  确保 SQLModel.metadata 注册兑换码三张表
from app.models import title as _title_models  # noqa: F401 触发 metadata 注册
from app.models import bot as _bot_models  # noqa: F401 触发 metadata 注册 bot_profile
from app.models import fx as _fx_models  # noqa: F401 触发 metadata 注册 FX 表
from app.models import credit as _credit_models  # noqa: F401 触发 metadata 注册 liquidation_run/action
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
from app.services.pve.scheduler import (
    start_scheduler as start_pve_scheduler,
    stop_scheduler as stop_pve_scheduler,
)
from app.services.fx.scheduler import (
    start_scheduler as start_fx_scheduler,
    stop_scheduler as stop_fx_scheduler,
)
from app.services.fx.publisher import start_publisher as start_fx_publisher, stop_publisher as stop_fx_publisher
from app.services.loan_migrate import auto_migrate
from app.services.credit import flags as credit_flags
from app.services.credit import ownership as credit_ownership

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
    # 阶段 4：不可变历史段，nginx 缓存回源为主，量大且无审计价值
    "/history/",
)


def _set_no_store_for_api(path: str, response):
    if path.startswith("/api/v1/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动/关闭编排。

    无论正常关闭还是启动中途失败，``finally`` 都执行 ``_shutdown()``：
    反向停止已启动资源 + 释放经济写所有权（reviewer blocker 6）。
    """
    try:
        await _startup(app)
        yield
    finally:
        await _shutdown()


async def _startup(app: FastAPI) -> None:
    # 启动顺序（WP3 单写所有权，spec §6.1 / 计划 WP3 "ownership"）：
    #   0) 只读实例声明 → 在**任何启动写之前**跳过 init_db/auto_migrate/seed/resync
    #   1) 非只读实例先用专用非池化 PG 连接取 pg_try_advisory_lock（启动写之前！）
    #   2) 只有 owner 才跑 init_db/auto_migrate（含 seed）
    #   3) 读 credit flags；unified_credit_enabled=true 却**非 owner 且非只读** → 启动失败
    #      （第二写实例必须被拒，不能回落只处理 LMSR 的 legacy 强平；只读实例不写，
    #       即使运营已开启统一信贷也允许启动）
    #   4) 只有 owner 才挂 SQLAdmin（它自带直写 API）、跑 resync / writer / flusher /
    #      全部写调度器；HTTP 经济写路径由 WP6 的 require_writes() 兜底
    main_logger = logging.getLogger("thccb.main")
    read_only = credit_flags.read_only_from_env()
    owner = False
    if read_only:
        credit_ownership.OWNERSHIP.mark_read_only()
        main_logger.warning(
            "read-only instance (%s): skip init_db/auto_migrate/resync/writer/flusher/"
            "all schedulers", credit_flags.READ_ONLY_ENV,
        )
    else:
        # 专用非池化连接：池化连接被回收会把 session advisory lock 带走
        owner = await credit_ownership.OWNERSHIP.acquire()
        if owner:
            await init_db()
            await auto_migrate()
        else:
            main_logger.critical(
                "single-writer ownership NOT acquired (%s): skip init_db/auto_migrate/"
                "resync/writer/flusher/all schedulers",
                credit_ownership.OWNERSHIP.reason,
            )
    # ── 统一信贷 flags（计划 §3.4）：启动时读一次 site_config；
    #    默认 unified_credit_enabled=false，开关关着时以下调度器/交易行为与本改动前一致。
    #    credit_new_risk_frozen 例外：风险检查运行期热读（见 flags.refresh_new_risk_frozen）。
    try:
        async with async_session_maker() as _flags_session:
            await credit_flags.load_flags(_flags_session)
    except Exception as exc:
        if not owner and not read_only:
            raise RuntimeError(
                "非 owner 实例无法读取 credit flags，拒绝启动"
                "（不能确认 unified_credit_enabled 是否要求持锁）"
            ) from exc
        raise
    flags = credit_flags.get_flags()
    if flags.unified_credit_enabled and not owner and not read_only:
        raise RuntimeError(
            "unified_credit_enabled=true 但本进程未持有经济写所有权"
            f"（{credit_ownership.OWNERSHIP.reason}）：拒绝以第二写实例启动"
        )
    writes_ok = owner and credit_flags.write_schedulers_enabled()
    app.state.credit_writes_enabled = writes_ok
    if writes_ok:
        # SQLAdmin 提供绕过业务校验的直写 API，只读/非 owner 实例不得挂载
        _configure_admin_economic_writes(flags.unified_credit_enabled)
        setup_admin(app, engine)
    else:
        main_logger.warning(
            "SQLAdmin 未挂载（read_only=%s owner=%s）：只读/非 owner 实例不得暴露"
            "直写 API", read_only, owner,
        )
    if writes_ok:
        # ── candle 表 race-window 兜底扫（spec § 6.3）──
        # 覆盖 migration→新代码上线之间可能漏的 buy/sell。
        # ★ 顺序依赖（阶段 4）：必须先于 WRITER.start()——writer 启动时从
        #   OutcomeCandle 回灌 HistoryRing，resync 先跑保证崩溃丢失的 ≤5s 已修复。
        try:
            await _resync_recent_candles()
        except Exception as e:
            # 兜底失败不能阻塞启动；记日志后续手工跑 backfill CLI
            logging.getLogger("thccb.candle").exception("resync_recent_candles failed: %s", e)
        # ── 写调度器：只读实例 / 非 owner 必须显式全关（spec §6.1）──
        await start_loan_scheduler()
        await start_liquidation_scheduler()
        await start_bot_detection_scheduler()
        # PvE 机器人引擎（spec 2026-08-29）：tick 内检查 pve_enabled 急停闸，默认关
        await start_pve_scheduler()
        await start_fx_scheduler()
        await start_fx_publisher()
    else:
        main_logger.warning(
            "writes disabled (read_only=%s owner=%s reason=%s): all write schedulers "
            "skipped", read_only, owner, credit_ownership.OWNERSHIP.reason,
        )
    # ── 单写者状态机（spec 2026-08-21 § 4）：启动时读 flag，翻转需重启 ──
    from app.services import site_config as _site_config
    from app.services.market_writer import WRITER
    from app.services.candle_flusher import CANDLE_FLUSHER
    if writes_ok:
        async with async_session_maker() as _s:
            _sw = await _site_config.get_bool_or(_s, "single_writer_enabled", False)
        if _sw:
            await WRITER.start()
            await CANDLE_FLUSHER.start()
    # ── 定频广播帧（spec § 5.1）：writer 与老路径共用，无条件启动（只读，不写库）──
    from app.services.tick_broadcaster import TICK_BROADCASTER
    await TICK_BROADCASTER.start()


def _configure_admin_economic_writes(unified: bool) -> None:
    """Route unified economic mutations through business services, not raw CRUD."""
    from app.core.admin import UserAdmin, MarketAdmin, OutcomeAdmin, PositionAdmin, TransactionAdmin
    for view in (UserAdmin, MarketAdmin, OutcomeAdmin, PositionAdmin, TransactionAdmin):
        view.can_create = not unified
        view.can_edit = not unified
        view.can_delete = not unified


async def _shutdown() -> None:
    # shutdown: 停 sweep + 释放连接池，避免优雅停机时残留连接
    # 启动顺序 loan → liquidation → bot_detection，停止时反序。
    # 调度器（含 liquidation sweep）必须先于 writer/flusher 停——反过来，在途 sweep
    # 会读到 WRITER.enabled=False 落回老路径，或在阶段 B 全部 submit 失败后写出
    # sold_positions_count=0 的假 LiquidationEvent（final review IMP-4）。
    # writer 再停（断新增命令）→ broadcaster 再停（此时 writer/老路径都不再 feed，
    # 做最后一次 flush 把残帧发给订阅者）→ flusher 最后停（做最终 flush），避免停
    # flusher 时 writer 仍在往 _pending 塞数据
    # PvE 最先停：它经回环 HTTP 下单，必须在 uvicorn 停止接收请求前住手
    #
    # reviewer blocker 6：每一步都 best-effort（单点失败不阻断后续清理），
    # 所有权释放与 engine.dispose 放在 finally，保证启动中途失败也会走到。
    logger = logging.getLogger("thccb.main")

    async def _safe(name: str, step) -> None:
        try:
            await step()
        except Exception:
            logger.exception("shutdown: %s 清理失败（继续其余清理）", name)

    try:
        await _safe("pve_scheduler", stop_pve_scheduler)
        await _safe("fx_scheduler", stop_fx_scheduler)
        await _safe("fx_publisher", stop_fx_publisher)
        await _safe("bot_detection_scheduler", stop_bot_detection_scheduler)
        await _safe("liquidation_scheduler", stop_liquidation_scheduler)
        await _safe("loan_scheduler", stop_loan_scheduler)
        from app.services.market_writer import WRITER as _writer
        from app.services.candle_flusher import CANDLE_FLUSHER as _flusher
        from app.services.tick_broadcaster import TICK_BROADCASTER as _tick_b
        await _safe("writer", _writer.stop)
        await _safe("tick_broadcaster", _tick_b.stop)  # writer 已停；最后 flush 发残帧
        await _safe("candle_flusher", _flusher.stop)
    finally:
        try:
            # 释放经济写所有权（专用连接随之关闭；PG 上 session lock 立即释放）
            await credit_ownership.OWNERSHIP.release()
        except Exception:
            logger.exception("shutdown: ownership release 失败")
        finally:
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

            # 滚动 prev_post_by_oid，每笔 tx 用上一笔的 post 当 pre_prices
            # （K-line fix `14bfd35` 要求 bucket 首笔 pre = 上 bucket 末笔 post 实现首尾相连）
            prev_post_by_oid: dict[int, float] = {
                oid: 1.0 / len(outcome_ids) for oid in outcome_ids
            }
            for tx in txs:
                if tx.market_prices_post and len(tx.market_prices_post) == len(outcome_ids):
                    new_prices = [float(p) for p in tx.market_prices_post]
                else:
                    new_prices = [
                        float(tx.post_market_price) if oid == tx.outcome_id
                        else 1.0 / len(outcome_ids) for oid in outcome_ids
                    ]
                pre_prices = [prev_post_by_oid[oid] for oid in outcome_ids]
                rows = compute_candle_rows(
                    traded_outcome_id=tx.outcome_id,
                    outcome_ids=outcome_ids,
                    pre_prices=pre_prices,
                    new_prices=new_prices,
                    traded_shares=tx.shares,
                    ts=tx.timestamp,
                )
                await upsert_candles(s, rows)
                for oid, p in zip(outcome_ids, new_prices):
                    prev_post_by_oid[oid] = p
            await s.commit()


app = FastAPI(title="东方炒炒币 (Touhou Exchange)", lifespan=lifespan)

# CORS — 从配置读取允许的源
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    # PATCH：admin 端（用户封禁/角色、PvE 机器人干预）用 PATCH 语义；
    # 生产同源不走 CORS，这里补齐是让跨域 dev（vite:5173 → 8004）也能用
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
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
app.include_router(fx_stream_api.router, prefix="/api/v1/fx", tags=["FX Stream"])

from app.api.v1 import history as history_api
app.include_router(history_api.router, prefix="/history", tags=["History"])  # 不在 /api/v1 下：绕开 no-store 中间件（见 history.py 模块注释）
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

from app.api.v1 import admin_bot as admin_bot_api
app.include_router(admin_bot_api.router, prefix="/api/v1/admin/bot", tags=["AdminBot"])

from app.api.v1 import admin_title as admin_title_api
app.include_router(admin_title_api.router, prefix="/api/v1/admin", tags=["AdminTitle"])

from app.api.v1 import admin_users as admin_users_api
app.include_router(admin_users_api.router, prefix="/api/v1/admin/users", tags=["AdminUsers"])

from app.api.v1 import admin_pve as admin_pve_api
app.include_router(admin_pve_api.router, prefix="/api/v1/admin/pve", tags=["AdminPve"])

from app.api.v1 import title as title_api
app.include_router(title_api.router, prefix="/api/v1/title", tags=["Title"])
app.include_router(fx_api.router, prefix="/api/v1/fx", tags=["FX"])
app.include_router(admin_fx_api.router, prefix="/api/v1/admin/fx", tags=["AdminFX"])


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
