import logging
import inspect
from functools import wraps
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Dict, Any, Optional, Tuple

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status, Query
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
import jwt
from app.core.config import settings
from app.services.rank import rank_title
from app.core.database import get_async_session
from app.core.users import current_active_user, current_superuser
from app.models.base import User, Market, Outcome, Transaction, MarketStatus, TransactionType
from app.schemas.market import (
    MarketCreate,
    TradeRequest,
    TradeResponse,
    ResolveRequest,
    SettleResult,
    MarketDetailRead,
    OutcomeQuoteRead,
    QuoteRequest,
    QuoteResponse,
    LeaderboardItem,
    MarketTradeRead,
    RecentTradeRead,
    MoverItem,
)
from app.services.lmsr import calculate_lmsr_with_prices, get_current_price, quantize_cost, seed_shares_from_prices, quantize_price
from app.services.fx.valuation import compute_total_net_worth
from app.services import site_config
from app.services import audit_service
from app.services.anti_bot import verify_client_token, parse_whitelist
from app.services.market_open import market_is_open
from app.services.credit.ownership import OWNERSHIP
from app.models.title import MarketRequiredTitle, Title as _TitleModel, UserTitle as _UserTitleModel


def _economic_market_write(fn):
    """Release the authentication read transaction before awaiting the writer."""
    signature = inspect.signature(fn)
    @wraps(fn)
    async def guarded(*args, **kwargs):
        from app.services.market_writer import WRITER
        OWNERSHIP.require_writes()
        if not WRITER.enabled:
            raise HTTPException(503, "市场写入服务不可用，请稍后重试")
        db = signature.bind(*args, **kwargs).arguments["db"]
        await db.close()
        return await fn(*args, **kwargs)
    return guarded


async def _optional_current_user_id(
    authorization: Optional[str] = Header(default=None),
) -> Optional[int]:
    """从 Authorization header 解出 user_id；解不出/无 token 返回 None。

    用于 market detail/list 这种公开但携 token 时要展示 user_can_trade 的端点。
    严格按 current_active_user 同一套 secret/algorithm 解码；不报 401（不要求登录）。
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        if payload.get("type") == "refresh":
            return None
        return int(payload["sub"])
    except Exception:
        return None

logger = logging.getLogger(__name__)

router = APIRouter()

ZERO = Decimal("0")

# ── Quote 进程内 TTL 缓存（perf）──
# quote 是无锁纯读 + LMSR 纯计算，前端用户每改一次数量就调一次，QPS 极高。
# 1s 内同 (outcome_id, quantized_shares, side) 的请求直接返回缓存，
# 省 3 次 DB 查询 + 2 次 LMSR cost + N+1 次 get_current_price。
#
# TTL 选 1s 的理由：
#   * 用户改数字典型 debounce 200-500ms，1s 窗口能 dedupe 一波 burst
#   * 灾难抛售场景下 stale 上限 1s，配合滑点保护（默认 5% / hardcap 10%）足够安全
#   * 再长（如 5s）灾难场景下 cache 可能持续返回严重过期的报价，导致用户成交被反复滑点拦截
#
# 不做主动失效（成交后清缓存），原因：
#   1. BROKER 是进程内 pubsub，跨 worker 通知要靠 Redis，引入复杂度
#   2. 1s TTL 已经够短——quote 本就是估算，不承诺成交价
#   3. 真正成交时 buy/sell 走真实 LMSR + 滑点保护，1s stale 不会让用户成交跑偏
# 多 worker 部署下每 worker 独立 cache，TTL 期内同请求最多算 N 次（worker 数），可接受。
_QUOTE_CACHE_TTL = 1.0
_QUOTE_CACHE_MAX = 5000  # dict 涨到这个阈值时触发一次性 GC 过期项
_QUOTE_CACHE: Dict[Tuple[int, str, str], Tuple[Any, float]] = {}


def _quote_cache_key(outcome_id: int, shares: Decimal, side: str) -> Tuple[int, str, str]:
    """quantize_cost 后转 str，规避 Decimal('1') vs Decimal('1.0') 差异。"""
    return (outcome_id, str(quantize_cost(shares)), side)


def _quote_cache_gc_if_full() -> None:
    """dict 涨到 _QUOTE_CACHE_MAX 时一次性清掉所有已过期项。"""
    if len(_QUOTE_CACHE) <= _QUOTE_CACHE_MAX:
        return
    now = time.monotonic()
    expired = [k for k, (_, exp) in _QUOTE_CACHE.items() if exp < now]
    for k in expired:
        _QUOTE_CACHE.pop(k, None)


# -----------------------------
# Helpers
# -----------------------------


async def _last_price_before(
    db: AsyncSession, outcome_ids: List[int], cutoff: datetime, price_col,
) -> Dict[int, float]:
    """每个 outcome 在 cutoff 之前的最后一笔成交的 price_col。无成交的不在 dict 中。

    用关联子查询 + LIMIT 1 而不是 row_number() 窗口函数：窗口函数要把 cutoff 前的
    **全部**历史成交按 outcome 排序再取第一行，O(全量成交)，而 /list（含 Docker
    healthcheck 每 30s）与详情页每次都跑；关联子查询让 Postgres 对每个 outcome 走
    ix_transaction_outcome_timestamp 倒序取 1 行，O(outcome 数)（审计 P1）。
    """
    if not outcome_ids:
        return {}
    sub = (
        select(price_col)
        .where(Transaction.outcome_id == Outcome.id, Transaction.timestamp <= cutoff)
        .order_by(Transaction.timestamp.desc(), Transaction.id.desc())
        .limit(1)
        .correlate(Outcome)
        .scalar_subquery()
    )
    res = await db.execute(select(Outcome.id, sub).where(Outcome.id.in_(outcome_ids)))
    return {row[0]: float(row[1]) for row in res.all() if row[1] is not None and float(row[1]) > 0}


async def _get_prices_24h_ago(db: AsyncSession, outcome_ids: List[int]) -> Dict[int, float]:
    """批量获取每个 outcome 在 24h 前的最后成交价。"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    return await _last_price_before(db, outcome_ids, cutoff, Transaction.price)


def _shares_to_floats(outcomes: List[Outcome]) -> List[float]:
    """Outcome.total_shares (Decimal) → float list，喂给 LMSR。"""
    return [float(o.total_shares) for o in outcomes]


def _build_prices_from_shares(
    outcomes: List[Outcome],
    shares_list: List[float],
    b: float,
    prices_24h_ago: Optional[Dict[int, float]] = None,
    prices: Optional[List[float]] = None,
) -> List[Dict[str, Any]]:
    out = []
    for i, o in enumerate(outcomes):
        p = prices[i] if prices is not None else get_current_price(shares_list, i, b)
        cur = float(quantize_price(p))
        entry: Dict[str, Any] = {
            "id": o.id,
            "label": o.label,
            "shares": float(o.total_shares),
            "current_price": cur,
        }
        if prices_24h_ago is not None:
            prev = prices_24h_ago.get(o.id)
            if prev is not None and prev > 0:
                entry["price_change_24h"] = round(cur - prev, 8)
                entry["price_change_pct_24h"] = round((cur - prev) / prev * 100, 2)
            else:
                entry["price_change_24h"] = None
                entry["price_change_pct_24h"] = None
        out.append(entry)
    return out


# ==========================================
# 管理员接口
# ==========================================

@router.post("/create", status_code=status.HTTP_201_CREATED, summary="创建新市场（仅限管理员）")
@_economic_market_write
async def create_market(
    data: MarketCreate,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    OWNERSHIP.require_writes()
    new_market = Market(
        title=data.title,
        description=data.description,
        liquidity_b=data.liquidity_b,
        status=MarketStatus.TRADING,
        closes_at=data.closes_at,
        tags=",".join(data.tags) if data.tags else "",
    )
    db.add(new_market)
    await db.flush()

    # 先验：q_i = b·(ln p_i − ln p_min)，使初始价等于 initial_prices；留空则全 0（均匀 1/N）
    if data.initial_prices:
        seed_q = seed_shares_from_prices(data.initial_prices, float(data.liquidity_b))
    else:
        seed_q = [ZERO] * len(data.outcomes)
    for label, q0 in zip(data.outcomes, seed_q):
        db.add(Outcome(market_id=new_market.id, label=label, total_shares=q0, initial_shares=q0))
    await db.flush()
    audit_service.record(
        db, "market_create", market_id=new_market.id, operator_user_id=admin.id,
        payload={"title": data.title, "liquidity_b": float(data.liquidity_b),
                 "outcomes": list(data.outcomes), "closes_at": data.closes_at,
                 "tags": list(data.tags or []),
                 "initial_prices": data.initial_prices,
                 "initial_shares": [str(x) for x in seed_q]},
        market_after=await audit_service.market_snapshot_from_db(db, int(new_market.id)),
    )

    await db.commit()

    from app.services.market_writer import WRITER
    await WRITER.register_market(int(new_market.id))

    return {
        "status": "success",
        "market_id": new_market.id,
        "title": new_market.title,
        "outcomes": data.outcomes,
        "created_by": admin.username,
    }


async def _settled_market_without_state(db: AsyncSession, market_id: int) -> Market:
    """Read terminal state after restart; an active market must have a writer."""
    market = await db.get(Market, market_id)
    if market is None:
        raise HTTPException(404, "市场不存在")
    if market.status != MarketStatus.SETTLED:
        raise HTTPException(503, "市场写入状态未就绪，请稍后重试")
    return market


@router.post("/{market_id:int}/close", summary="关闭市场交易（仅限管理员）")
@_economic_market_write
async def close_market(
    market_id: int,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    from app.services.market_writer import WRITER
    from app.services.writer_ops import CloseCmd
    if WRITER.get_state(market_id) is not None:
        return await WRITER.submit(CloseCmd(market_id=market_id, admin_id=int(admin.id)))
    await _settled_market_without_state(db, market_id)
    raise HTTPException(400, "市场已结算，无法熔断")


# ==========================================
# 公共接口
# ==========================================

@router.get("/list", summary="获取所有活跃市场")
async def list_markets(
    keyword: str = Query(None, description="按标题搜索"),
    tag: str = Query(None, description="按标签过滤"),
    include_halt: bool = Query(False, description="是否包含熔断中的市场"),
    include_settled: bool = Query(False, description="是否包含已结算的市场"),
    db: AsyncSession = Depends(get_async_session),
):
    allowed = [MarketStatus.TRADING]
    if include_halt:
        allowed.append(MarketStatus.HALT)
    if include_settled:
        allowed.append(MarketStatus.SETTLED)

    stmt = (
        select(Market)
        .where(Market.status.in_(allowed))
        .options(selectinload(Market.outcomes))
        .order_by(Market.created_at.desc())
    )

    if keyword:
        stmt = stmt.where(Market.title.contains(keyword))
    if tag:
        stmt = stmt.where(Market.tags.contains(tag))

    res = await db.execute(stmt)
    markets = res.scalars().all()

    # 批量查 24h 前价格
    all_outcome_ids = [o.id for m in markets for o in m.outcomes]
    prices_24h = await _get_prices_24h_ago(db, all_outcome_ids)

    # 批量聚合：每个 market 的成交笔数 + 最后成交时间。
    # 只统计真实用户交易（buy/sell），结算产生的 settle/settle_lose 不算活跃度。
    market_ids = [m.id for m in markets]
    activity: Dict[int, Dict[str, Any]] = {}
    if market_ids:
        agg_stmt = (
            select(
                Outcome.market_id.label("market_id"),
                func.count(Transaction.id).label("trade_count"),
                func.max(Transaction.timestamp).label("last_trade_at"),
            )
            .select_from(Outcome)
            .outerjoin(
                Transaction,
                and_(
                    Transaction.outcome_id == Outcome.id,
                    Transaction.type.in_([TransactionType.BUY, TransactionType.SELL]),
                ),
            )
            .where(Outcome.market_id.in_(market_ids))
            .group_by(Outcome.market_id)
        )
        for row in (await db.execute(agg_stmt)).all():
            activity[row.market_id] = {
                "trade_count": int(row.trade_count or 0),
                "last_trade_at": row.last_trade_at,
            }

    # Title 系统（Task 13）：批量查每个 market 的 required_titles chip
    required_by_market: Dict[int, List[Dict[str, Any]]] = {}
    if market_ids:
        mrt_rows = (await db.execute(
            select(
                MarketRequiredTitle.market_id,
                _TitleModel.id,
                _TitleModel.name,
                _TitleModel.color,
                _TitleModel.icon,
                _TitleModel.sort_order,
            )
            .join(_TitleModel, MarketRequiredTitle.title_id == _TitleModel.id)
            .where(MarketRequiredTitle.market_id.in_(market_ids))
            .order_by(_TitleModel.sort_order.asc(), _TitleModel.id.asc())
        )).all()
        for row in mrt_rows:
            required_by_market.setdefault(row[0], []).append(
                {"id": row[1], "name": row[2], "color": row[3], "icon": row[4]}
            )

    output = []
    for m in markets:
        outcomes = list(m.outcomes)
        shares_list = _shares_to_floats(outcomes)
        tags_list = [t.strip() for t in m.tags.split(",") if t.strip()] if m.tags else []
        act = activity.get(m.id, {})
        last_ts = act.get("last_trade_at")
        output.append({
            "id": m.id,
            "title": m.title,
            "description": m.description,
            "liquidity_b": float(m.liquidity_b),
            "status": m.status,
            "closes_at": m.closes_at.isoformat() if m.closes_at else None,
            "tags": tags_list,
            "outcomes": _build_prices_from_shares(outcomes, shares_list, float(m.liquidity_b), prices_24h),
            "trade_count": act.get("trade_count", 0),
            "last_trade_at": last_ts.isoformat() if last_ts else None,
            "required_titles": required_by_market.get(m.id, []),
        })
    return output


@router.get("/{market_id:int}", response_model=MarketDetailRead, summary="获取市场详情")
async def get_market_detail(
    market_id: int,
    db: AsyncSession = Depends(get_async_session),
    current_user_id: Optional[int] = Depends(_optional_current_user_id),
):
    market = await db.get(Market, market_id)
    if not market:
        raise HTTPException(status_code=404, detail="市场不存在")

    o_res = await db.execute(
        select(Outcome)
        .where(Outcome.market_id == market.id)
        .order_by(Outcome.id.asc())
    )
    outcomes = o_res.scalars().all()
    if not outcomes:
        raise HTTPException(status_code=400, detail="市场选项异常：无 outcomes")

    shares_list = _shares_to_floats(outcomes)
    b = float(market.liquidity_b)

    # 24h 前价格
    outcome_ids = [o.id for o in outcomes]
    prices_24h = await _get_prices_24h_ago(db, outcome_ids)

    out_reads: list[OutcomeQuoteRead] = []
    for i, o in enumerate(outcomes):
        price = quantize_price(get_current_price(shares_list, i, b))
        is_winner = None
        if getattr(market, "winning_outcome_id", None) is not None:
            is_winner = (int(market.winning_outcome_id) == int(o.id))

        prev = prices_24h.get(o.id)
        cur_f = float(price)
        if prev is not None and prev > 0:
            change = round(cur_f - prev, 8)
            change_pct = round((cur_f - prev) / prev * 100, 2)
        else:
            change = None
            change_pct = None

        out_reads.append(
            OutcomeQuoteRead(
                id=int(o.id),
                label=str(o.label),
                total_shares=o.total_shares,
                initial_shares=o.initial_shares,
                current_price=price,
                payout=o.payout,
                is_winner=is_winner,
                price_change_24h=change,
                price_change_pct_24h=change_pct,
            )
        )

    # last_trade_at（Transaction 无 market_id，经 Outcome join 到 market）
    tx_stmt = (
        select(Transaction.timestamp)
        .join(Outcome, Transaction.outcome_id == Outcome.id)
        .where(Outcome.market_id == market.id)
        .order_by(Transaction.timestamp.desc())
        .limit(1)
    )
    tx_res = await db.execute(tx_stmt)
    last_trade_at = tx_res.scalar_one_or_none()

    # Title 系统（Task 13）：required_titles chip + 当前 user 是否能交易
    required_title_rows = (await db.execute(
        select(_TitleModel)
        .join(MarketRequiredTitle, MarketRequiredTitle.title_id == _TitleModel.id)
        .where(MarketRequiredTitle.market_id == market.id)
        .order_by(_TitleModel.sort_order.asc(), _TitleModel.id.asc())
    )).scalars().all()
    required_titles_chips = [
        {"id": t.id, "name": t.name, "color": t.color, "icon": t.icon}
        for t in required_title_rows
    ]
    if not required_titles_chips:
        user_can_trade = True
    elif current_user_id is None:
        user_can_trade = False
    else:
        has = (await db.execute(
            select(_UserTitleModel.title_id).where(
                _UserTitleModel.user_id == current_user_id,
                _UserTitleModel.title_id.in_([rt["id"] for rt in required_titles_chips]),
            )
        )).scalars().first()
        user_can_trade = has is not None

    return MarketDetailRead(
        id=int(market.id),
        title=str(market.title),
        description=str(market.description or ""),
        status=str(market.status),
        liquidity_b=float(market.liquidity_b),
        created_at=market.created_at.replace(tzinfo=timezone.utc) if market.created_at.tzinfo is None else market.created_at,
        closes_at=market.closes_at,
        tags=[t.strip() for t in market.tags.split(",") if t.strip()] if market.tags else [],

        winning_outcome_id=getattr(market, "winning_outcome_id", None),
        settled_at=getattr(market, "settled_at", None),
        settled_by_user_id=getattr(market, "settled_by_user_id", None),

        outcomes=out_reads,
        last_trade_at=last_trade_at,
        required_titles=required_titles_chips,
        user_can_trade=user_can_trade,
    )


# ==========================================
# 交易接口（登录用户）
# ==========================================


async def verify_anti_bot(
    request: Request,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
) -> User:
    """L2 anti-bot 验证 + L3 白名单。

    1. activity_mode_enabled=false → 通过 (默认 / 平时)
    2. user 在 quant_whitelist_user_ids → 通过 (自己 quant 例外)
    3. 否则验 HMAC X-Client-Token + X-Client-TS，失败 403
    """
    if not await site_config.get_bool(db, "activity_mode_enabled"):
        return user

    try:
        whitelist_csv = await site_config.get_str(db, "quant_whitelist_user_ids")
    except site_config.SiteConfigError:
        whitelist_csv = ""
    if user.id in parse_whitelist(whitelist_csv):
        return user

    token = request.headers.get("X-Client-Token")
    ts = request.headers.get("X-Client-TS")
    if not token or not ts:
        raise HTTPException(403, detail="anti-bot 验证缺失（活动期间禁止脚本访问）")

    if not verify_client_token(token, ts, user.id):
        raise HTTPException(403, detail="anti-bot 验证失败")
    return user


async def _release_request_connection(db: AsyncSession) -> None:
    """writer 路径提交命令前把请求 session 占用的池连接还回去。

    依赖注入里 get_current_user / verify_anti_bot 已经用这条 session 查过库，
    SQLAlchemy 自动开了事务并把连接一直占到请求结束；随后 `await WRITER.submit()`
    最长等 10s，这段时间连接纯闲置。池 = 10 + overflow 20，10 r/s 的买卖下 DB 一抖
    3 秒就能把池抽干，writer op 自己反而拿不到连接 → 级联 503「结果未知」。
    close() 回滚（只读，无副作用）并归还连接；user 对象已加载且 expire_on_commit=False，
    之后只读 id/username 不会再触发 SQL。
    """
    await db.close()


@router.post("/buy", response_model=TradeResponse, summary="买入胜券")
@_economic_market_write
async def buy_shares(
    req: TradeRequest,
    user: User = Depends(verify_anti_bot),
    db: AsyncSession = Depends(get_async_session),
):
    shares_d = quantize_cost(req.shares)
    if shares_d <= ZERO:
        raise HTTPException(status_code=422, detail="shares 必须为正数")

    from app.services.market_writer import WRITER
    from app.services.writer_ops import BuyCmd
    mid = WRITER.market_id_for_outcome(int(req.outcome_id))
    if mid is None:
        row = await db.execute(
            select(Outcome.market_id).where(Outcome.id == int(req.outcome_id)))
        if row.scalars().first() is None:
            raise HTTPException(status_code=404, detail="选项不存在")
        # outcome 存在，但市场没有可交易的 writer state
        raise HTTPException(status_code=400, detail="市场当前不可交易")
    await _release_request_connection(db)
    return await WRITER.submit(BuyCmd(
        market_id=mid,
        outcome_id=int(req.outcome_id),
        user_id=int(user.id),
        username=user.username,
        shares=shares_d,
        max_cost=req.max_cost,
        max_slippage_bps=req.max_slippage_bps,
        accept_any_slippage=req.accept_any_slippage,
    ))


@router.post("/sell", response_model=TradeResponse, summary="卖出胜券")
@_economic_market_write
async def sell_shares(
    req: TradeRequest,
    user: User = Depends(verify_anti_bot),
    db: AsyncSession = Depends(get_async_session),
):
    shares_d = quantize_cost(req.shares)
    if shares_d <= ZERO:
        raise HTTPException(status_code=422, detail="shares 必须为正数")

    from app.services.market_writer import WRITER
    from app.services.writer_ops import SellCmd
    mid = WRITER.market_id_for_outcome(int(req.outcome_id))
    if mid is None:
        row = await db.execute(
            select(Outcome.market_id).where(Outcome.id == int(req.outcome_id)))
        if row.scalars().first() is None:
            raise HTTPException(status_code=404, detail="选项不存在")
        # outcome 存在，但市场没有可交易的 writer state
        raise HTTPException(status_code=400, detail="市场当前不可交易")
    await _release_request_connection(db)
    return await WRITER.submit(SellCmd(
        market_id=mid,
        outcome_id=int(req.outcome_id),
        user_id=int(user.id),
        username=user.username,
        shares=shares_d,
        min_proceeds=req.min_proceeds,
        max_slippage_bps=req.max_slippage_bps,
        accept_any_slippage=req.accept_any_slippage,
    ))


@router.post(
    "/{market_id:int}/resolve",
    response_model=SettleResult,
    summary="结算市场（指定赢家，发放兑付，仅管理员）",
    status_code=status.HTTP_200_OK,
)
@_economic_market_write
async def resolve_market(
    market_id: int,
    req: ResolveRequest,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    from app.services.market_writer import WRITER
    from app.services.writer_ops import ResolveCmd
    if WRITER.get_state(market_id) is not None:
        return await WRITER.submit(ResolveCmd(
            market_id=market_id, winning_outcome_id=req.winning_outcome_id,
            payout=req.payout, admin_id=int(admin.id),
        ))
    if quantize_cost(req.payout) < ZERO:
        raise HTTPException(422, "payout 必须 >= 0")
    market = await _settled_market_without_state(db, market_id)
    if market.winning_outcome_id is None or market.settled_at is None:
        raise HTTPException(500, "市场已结算但结算字段缺失（数据异常）")
    return SettleResult(
        market_id=market.id, status=market.status,
        winning_outcome_id=int(market.winning_outcome_id), settled_at=market.settled_at,
        total_payout=ZERO, settled_positions=0,
    )


@router.post("/quote", response_model=QuoteResponse, summary="下单预估（不真实成交）")
async def quote_trade(
    req: QuoteRequest,
    user: User = Depends(verify_anti_bot),
    db: AsyncSession = Depends(get_async_session),
):
    # ── 缓存命中 fast path ──
    # 注意：cache 命中跳过 market_status check。admin 关市场是极少操作，
    # 1s 内仍返回旧 quote 可接受；用户真的下单时 buy/sell 会自己拦截关市场后的请求。
    cache_key = _quote_cache_key(req.outcome_id, req.shares, req.side)
    cached = _QUOTE_CACHE.get(cache_key)
    if cached is not None:
        cached_resp, expires = cached
        if time.monotonic() < expires:
            return cached_resp
        _QUOTE_CACHE.pop(cache_key, None)

    # ── 取市场状态：写实例使用内存权威 q/b/status；只读实例和历史市场读取 DB ──
    from app.services.market_writer import WRITER
    ids: List[int]; labels: List[str]; old_q: List[float]
    st = None
    if WRITER.enabled:
        mid = WRITER.market_id_for_outcome(int(req.outcome_id))
        st = WRITER.get_state(mid) if mid is not None else None
    if st is not None and not st.unavailable:
        if not market_is_open(st.status, st.closes_at):
            raise HTTPException(status_code=400, detail="市场未在交易中，无法报价")
        ids, labels, old_q, b = st.outcome_ids, st.outcome_labels, list(st.q), st.b
    else:
        outcome = await db.get(Outcome, req.outcome_id)
        if not outcome:
            raise HTTPException(status_code=404, detail="选项不存在")
        market = await db.get(Market, outcome.market_id)
        if not market:
            raise HTTPException(status_code=404, detail="市场不存在")
        # 与 buy/sell 同源：status + closes_at 一起判（审计 L8）
        if not market_is_open(market.status, market.closes_at):
            raise HTTPException(status_code=400, detail="市场未在交易中，无法报价")
        outcomes = (await db.execute(
            select(Outcome).where(Outcome.market_id == market.id).order_by(Outcome.id)
        )).scalars().all()
        ids = [int(o.id) for o in outcomes]
        labels = [o.label for o in outcomes]
        old_q = _shares_to_floats(outcomes)
        b = float(market.liquidity_b)

    idx = next((i for i, oid in enumerate(ids) if oid == int(req.outcome_id)), None)
    if idx is None:
        raise HTTPException(status_code=400, detail="选项不属于该市场（数据异常）")
    shares_d = quantize_cost(req.shares)    # Decimal，用于精确计算
    shares_f = float(shares_d)              # 量化后再转 float，与成交同源（审计 L8）

    old_cost, _old_prices = calculate_lmsr_with_prices(old_q, b)

    new_q = list(old_q)
    if req.side == "buy":
        new_q[idx] += shares_f
        new_cost, new_prices = calculate_lmsr_with_prices(new_q, b)
        gross = quantize_cost(new_cost - old_cost)
        fee = ZERO
        net = gross
    else:
        if new_q[idx] < shares_f:
            raise HTTPException(status_code=400, detail="卖出数量超过市场总份额")
        new_q[idx] -= shares_f
        new_cost, new_prices = calculate_lmsr_with_prices(new_q, b)
        gross = quantize_cost(old_cost - new_cost)
        sell_fee_rate = await site_config.get_decimal_or(db, "sell_fee_rate", ZERO)
        fee = (gross * sell_fee_rate).quantize(Decimal("0.000001"))
        net = gross - fee

    avg_price = quantize_price(gross / shares_d) if shares_d > ZERO else ZERO
    after_prices = [
        {"id": oid, "label": lab, "shares": float(quantize_cost(new_q[i])),
         "current_price": float(quantize_price(new_prices[i]))}
        for i, (oid, lab) in enumerate(zip(ids, labels))
    ]

    response = QuoteResponse(
        outcome_id=req.outcome_id,
        side=req.side,
        shares=shares_d,
        avg_price=avg_price,
        gross=gross,
        fee=fee,
        net=net,
        after_prices=after_prices,
    )

    # ── 写入缓存 ──
    _QUOTE_CACHE[cache_key] = (response, time.monotonic() + _QUOTE_CACHE_TTL)
    _quote_cache_gc_if_full()

    return response


@router.get(
    "/{market_id:int}/trades",
    response_model=List[MarketTradeRead],
    summary="市场逐笔成交"
)
async def get_market_trades(
    market_id: int,
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_async_session),
):
    stmt = (
        select(Transaction, User)
        .join(Outcome, Transaction.outcome_id == Outcome.id)
        .join(User, Transaction.user_id == User.id)
        .where(Outcome.market_id == market_id)
        .order_by(Transaction.timestamp.desc())
        .limit(limit)
    )
    res = await db.execute(stmt)
    rows = res.all()

    return [
        MarketTradeRead(
            id=tx.id,
            outcome_id=tx.outcome_id,
            side=tx.type,
            shares=tx.shares,
            price=tx.price,
            gross=tx.gross,
            fee=tx.fee,
            timestamp=tx.timestamp.replace(tzinfo=timezone.utc) if tx.timestamp.tzinfo is None else tx.timestamp,
            username=u.username,
        )
        for tx, u in rows
    ]


@router.post("/{market_id:int}/resume", summary="恢复市场交易（仅管理员）")
@_economic_market_write
async def resume_market(
    market_id: int,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    from app.services.market_writer import WRITER
    from app.services.writer_ops import ResumeCmd
    if WRITER.get_state(market_id) is not None:
        return await WRITER.submit(ResumeCmd(market_id=market_id, admin_id=int(admin.id)))
    await _settled_market_without_state(db, market_id)
    raise HTTPException(400, "市场已结算，无法恢复交易")


# ── Leaderboard 进程内 TTL 缓存（审计 P2）──
# net_worth 模式每次请求全量 User + 全量 Position + 全量 Outcome + 逐仓 LMSR，匿名可访问、
# 无限速，是最容易被刷的 CPU 热点。排行榜本就不承诺实时，10s 内复用同一结果。
_LEADERBOARD_CACHE_TTL = 10.0
_LEADERBOARD_CACHE: Dict[Tuple[str, int], Tuple[Any, float]] = {}


def clear_leaderboard_cache() -> None:
    _LEADERBOARD_CACHE.clear()


@router.get("/leaderboard", response_model=List[LeaderboardItem], summary="财富/消费排行榜")
async def leaderboard(
    limit: int = Query(20, ge=1, le=100),
    mode: str = Query("net_worth", description="net_worth=cash-debt+持仓 LMSR 清算价；spending=兑换消费总额-当前债务"),
    db: AsyncSession = Depends(get_async_session),
):
    key = (mode, limit)
    hit = _LEADERBOARD_CACHE.get(key)
    if hit is not None and hit[1] > time.monotonic():
        return hit[0]
    result = await _leaderboard_uncached(limit, mode, db)
    _LEADERBOARD_CACHE[key] = (result, time.monotonic() + _LEADERBOARD_CACHE_TTL)
    return result


async def _leaderboard_uncached(limit: int, mode: str, db: AsyncSession):
    # PvE：机器人默认参与排名（把 NPC 从榜上打下去是玩法）；可用开关排除
    include_bots = await site_config.get_bool_or(db, "leaderboard_include_bots", True)
    if mode == "net_worth":
        # 排行榜按 MTM 口径排序（瞬时价 × 数量），跟 /user/summary 主显示一致，
        # 用户对自己排名的认知 = 看到的"我的净资产"。LCV 更保守但偏低不直观,
        # 不适合做排名口径。详见 docs/holdings-value-semantics.md。
        users_stmt = select(User)
        if not include_bots:
            users_stmt = users_stmt.where(User.is_bot == False)  # noqa: E712
        users = (await db.execute(users_stmt)).scalars().all()
        if not users:
            return []
        net_worth = await compute_total_net_worth(db, user_ids=[u.id for u in users])
        scored = [(u, net_worth[u.id]) for u in users if net_worth.get(u.id) is not None]
        scored.sort(key=lambda x: x[1], reverse=True)
        top = scored[:limit]
        # 批量取 equipped title chip：只查 top N 的 equipped_title_id,避免 N+1
        title_ids = {u.equipped_title_id for u, _ in top if u.equipped_title_id}
        title_map: Dict[int, _TitleModel] = {}
        if title_ids:
            rows = (await db.execute(
                select(_TitleModel).where(_TitleModel.id.in_(title_ids))
            )).scalars().all()
            title_map = {t.id: t for t in rows}
        return [
            LeaderboardItem(
                user_id=u.id,
                username=u.username,
                net_worth=nw.quantize(Decimal("0.01")),
                rank=rank_title(nw),
                equipped_title=(
                    {
                        "id": title_map[u.equipped_title_id].id,
                        "name": title_map[u.equipped_title_id].name,
                        "color": title_map[u.equipped_title_id].color,
                        "icon": title_map[u.equipped_title_id].icon,
                    }
                    if u.equipped_title_id and u.equipped_title_id in title_map
                    else None
                ),
            )
            for u, nw in top
        ]

    if mode == "spending":
        # 子查询聚合两张消费来源表：现有 redemption_transaction（partner 兑换）+
        # 新增 danmuku_exchange（弹幕系统兑换）。两边各 SUM 后外连接到 user，
        # COALESCE 把没消费过的用户的 NULL 归零。
        from app.models.redemption import RedemptionTransaction, DanmukuExchange

        rt_subq = (
            select(
                RedemptionTransaction.user_id.label("user_id"),
                func.sum(RedemptionTransaction.amount).label("rt_sum"),
            )
            .group_by(RedemptionTransaction.user_id)
            .subquery()
        )
        de_subq = (
            select(
                DanmukuExchange.user_id.label("user_id"),
                func.sum(DanmukuExchange.amount).label("de_sum"),
            )
            .group_by(DanmukuExchange.user_id)
            .subquery()
        )

        spent_expr = (
            func.coalesce(rt_subq.c.rt_sum, 0) + func.coalesce(de_subq.c.de_sum, 0)
        )
        score_expr = spent_expr - User.debt

        stmt = (
            select(
                User.id,
                User.username,
                User.debt,
                spent_expr.label("spent_total"),
                score_expr.label("score"),
                _TitleModel.id.label("title_id"),
                _TitleModel.name.label("title_name"),
                _TitleModel.color.label("title_color"),
                _TitleModel.icon.label("title_icon"),
            )
            .outerjoin(rt_subq, rt_subq.c.user_id == User.id)
            .outerjoin(de_subq, de_subq.c.user_id == User.id)
            .outerjoin(_TitleModel, _TitleModel.id == User.equipped_title_id)
            .order_by(score_expr.desc())
            .limit(limit)
        )
        if not include_bots:
            stmt = stmt.where(User.is_bot == False)  # noqa: E712
        rows = (await db.execute(stmt)).all()

        items: List[LeaderboardItem] = []
        for r in rows:
            spent = Decimal(str(r.spent_total or 0))
            debt = Decimal(str(r.debt or 0))
            score = Decimal(str(r.score or 0))
            # 完全没消费过的用户不参与 spending 榜（score = -debt 没意义，会拉一堆 0 消费 +
            # 负债的用户上来；过滤 spent > 0 让榜单只展示有过实际消费的）
            if spent <= 0:
                continue
            equipped = (
                {
                    "id": r.title_id,
                    "name": r.title_name,
                    "color": r.title_color,
                    "icon": r.title_icon,
                }
                if r.title_id is not None
                else None
            )
            items.append(LeaderboardItem(
                user_id=r.id,
                username=r.username,
                net_worth=score.quantize(Decimal("0.01")),
                rank=rank_title(score),
                spent_total=spent.quantize(Decimal("0.01")),
                debt=debt.quantize(Decimal("0.01")),
                equipped_title=equipped,
            ))
        return items

    raise HTTPException(status_code=400, detail="mode 取值仅支持 net_worth | spending")


# ==========================================
# 首页：实时成交流 + 涨跌榜
# ==========================================

@router.get("/recent-trades", response_model=List[RecentTradeRead], summary="跨市场最近成交")
async def recent_trades(
    limit: int = Query(30, ge=1, le=100),
    db: AsyncSession = Depends(get_async_session),
):
    """返回最近 N 笔买/卖成交（不含结算系统单），跨所有市场，倒序按时间。"""
    stmt = (
        select(Transaction, Outcome, Market, User)
        .join(Outcome, Transaction.outcome_id == Outcome.id)
        .join(Market, Outcome.market_id == Market.id)
        .join(User, Transaction.user_id == User.id)
        .where(Transaction.type.in_([TransactionType.BUY, TransactionType.SELL]))
        .order_by(Transaction.timestamp.desc())
        .limit(limit)
    )
    res = await db.execute(stmt)
    rows = res.all()
    return [
        RecentTradeRead(
            id=tx.id,
            timestamp=tx.timestamp.replace(tzinfo=timezone.utc) if tx.timestamp.tzinfo is None else tx.timestamp,
            market_id=mk.id,
            market_title=mk.title,
            outcome_id=oc.id,
            outcome_label=oc.label,
            type=tx.type,
            shares=tx.shares,
            price=tx.price,
            username=u.username,
        )
        for tx, oc, mk, u in rows
    ]


_MOVER_WINDOW_SECONDS: Dict[str, int] = {
    "10min": 600,
    "1h": 3600,
    "24h": 86400,
}


@router.get("/movers", response_model=List[MoverItem], summary="涨跌榜（按时间窗口）")
async def movers(
    window: str = Query("24h", pattern="^(10min|1h|24h)$", description="时间窗口"),
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_async_session),
):
    """
    返回所有 trading 市场所有 outcome 的价格变动榜，按 |change_pct| 倒序。
    基准价：每个 outcome 在 cutoff 之前最后一笔交易的 post_market_price；
    若该 outcome 无 cutoff 之前的交易记录，则回退到初始等概率 1/N（仅当
    整个市场也无 cutoff 之前交易时严格正确，否则为近似）。
    """
    seconds = _MOVER_WINDOW_SECONDS[window]
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=seconds)

    # 1) 取所有 trading 市场
    m_res = await db.execute(
        select(Market)
        .where(Market.status == MarketStatus.TRADING)
        .options(selectinload(Market.outcomes))
    )
    markets = m_res.scalars().all()
    if not markets:
        return []

    # 2) 批量查每个 outcome 在 cutoff 前的最后一笔成交价
    all_outcome_ids = [o.id for m in markets for o in m.outcomes]
    if not all_outcome_ids:
        return []
    prices_then = await _last_price_before(db, all_outcome_ids, cutoff, Transaction.post_market_price)

    # 3) 计算每个 outcome 的当前价 + 变化
    results: List[MoverItem] = []
    for m in markets:
        outcomes = list(m.outcomes)
        n = len(outcomes)
        if n < 2:
            continue
        shares_list = _shares_to_floats(outcomes)
        b = float(m.liquidity_b)
        for i, o in enumerate(outcomes):
            cur = float(quantize_price(get_current_price(shares_list, i, b)))
            then = prices_then.get(o.id, 1.0 / n)  # 无历史则用初始等概率
            if then <= 0:
                continue
            change_pct = round((cur - then) / then * 100, 2)
            results.append(MoverItem(
                market_id=int(m.id),
                market_title=str(m.title),
                outcome_id=int(o.id),
                outcome_label=str(o.label),
                price_now=quantize_price(cur),
                price_then=quantize_price(then),
                change_pct=change_pct,
            ))

    # 4) 按 |change_pct| 倒序，取 top N（前端再切分涨/跌）
    results.sort(key=lambda x: abs(x.change_pct), reverse=True)
    return results[:limit]
