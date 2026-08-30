"""构建 MarketView 快照：一次市场查询 + 一次近 60min 成交查询，全体机器人共享。

另含管理员风向注入的解析：site_config `pve_sentiment`（在 /admin/pve 全局配置里编辑），
格式 `{"tilts": {"<outcome_id>": 0.15}, "expires_at": "<ISO 时间，可省=一直生效>"}`，
believer 系模板把 tilt 加进长线 edge（运营可借此制造事件驱动行情）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.base import Market, MarketStatus, Transaction
from app.services import lmsr, site_config
from app.services.pve.templates import MarketBrief, MarketView, OutcomeView, TradeBrief

_TRADES_WINDOW_MIN = 60
_TRADES_LIMIT = 600


def parse_sentiment(raw: Optional[str], now: datetime) -> Dict[int, float]:
    """解析风向配置。过期 / 任何格式错误 → {}（绝不让一条配置炸掉整个 tick）。"""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        exp = data.get("expires_at")
        if exp:
            exp_dt = datetime.fromisoformat(exp)
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            if now >= exp_dt:
                return {}
        return {int(k): float(v) for k, v in (data.get("tilts") or {}).items()}
    except (ValueError, TypeError, AttributeError):
        return {}


async def build_market_view(db: AsyncSession) -> MarketView:
    now = datetime.now(timezone.utc)
    markets = (
        (
            await db.execute(
                select(Market)
                .where(Market.status == MarketStatus.TRADING)
                .options(selectinload(Market.outcomes))
            )
        )
        .scalars()
        .all()
    )
    outcomes: dict[int, OutcomeView] = {}
    briefs: dict[int, MarketBrief] = {}
    for m in markets:
        ordered = sorted(m.outcomes, key=lambda o: o.id)
        shares_list = [float(o.total_shares) for o in ordered]
        briefs[m.id] = MarketBrief(
            market_id=m.id,
            outcome_ids=[o.id for o in ordered],
            liquidity_b=float(m.liquidity_b),
        )
        for i, o in enumerate(ordered):
            outcomes[o.id] = OutcomeView(
                outcome_id=o.id,
                market_id=m.id,
                label=o.label,
                price=lmsr.get_current_price(shares_list, i, m.liquidity_b),
            )

    cutoff = now - timedelta(minutes=_TRADES_WINDOW_MIN)
    txs = (
        (
            await db.execute(
                select(Transaction)
                .where(Transaction.timestamp >= cutoff, Transaction.type.in_(("buy", "sell")))
                .order_by(Transaction.timestamp.desc())
                .limit(_TRADES_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    oid2mid = {oid: mid for mid, mb in briefs.items() for oid in mb.outcome_ids}
    trades = [
        TradeBrief(
            # sqlite（测试）返回 naive datetime，统一补 UTC，避免与 aware now 比较崩
            ts=t.timestamp if t.timestamp.tzinfo else t.timestamp.replace(tzinfo=timezone.utc),
            outcome_id=t.outcome_id,
            market_id=oid2mid[t.outcome_id],
            side=t.type,
            shares=float(t.shares),
            price=float(t.price),
            market_prices_post=t.market_prices_post,
        )
        for t in txs
        if t.outcome_id in oid2mid  # 已结算/halt 市场的历史成交不进快照
    ]
    try:
        raw = await site_config.get_str(db, "pve_sentiment")
    except site_config.SiteConfigError:
        raw = None
    return MarketView(
        now=now, outcomes=outcomes, markets=briefs, trades=trades,
        sentiment=parse_sentiment(raw, now),
    )
