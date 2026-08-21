"""单写者内存状态机（spec § 4）。

每个市场一条 asyncio.Queue + 常驻 consumer task；内存 q 是权威，
DB outcome.total_shares 是镜像（每笔 commit 内同步写、值恒等，
spec § 4.4 的 6dp 不动点）。启用与否在进程启动时决定（翻转需重启）。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.base import Market, MarketStatus, Outcome
from app.services.lmsr import calculate_lmsr_with_prices, quantize_cost

logger = logging.getLogger(__name__)


@dataclass
class MarketState:
    market_id: int
    b: float
    outcome_ids: list[int]        # 升序——价格向量的索引契约（spec § 4.1）
    outcome_labels: list[str]     # 与 outcome_ids 同序，buy/sell 响应 message 用
    q_dec: list[Decimal]          # 权威值 = DB 镜像的 6dp 量化结果（不动点，spec § 4.4）
    q: list[float]                # q_dec 的 float 派生，喂 LMSR
    prices: list[float]           # 由 q 导出并缓存
    status: MarketStatus
    closes_at: Optional[datetime]
    seq: int = 0                  # 帧序号，阶段 2（定频广播）才使用
    unavailable: bool = False     # 自愈失败后置 True：该市场一律 503（spec § 4.4 异常策略）


def _derive(q_dec: list[Decimal], b: float) -> tuple[list[float], list[float]]:
    """q_dec → (q floats, prices)。"""
    q = [float(x) for x in q_dec]
    _, prices = calculate_lmsr_with_prices(q, b)
    return q, prices


async def _load_one(session, market: Market) -> MarketState:
    outs = (await session.execute(
        select(Outcome).where(Outcome.market_id == market.id).order_by(Outcome.id.asc())
    )).scalars().all()
    q_dec = [quantize_cost(o.total_shares) for o in outs]
    q, prices = _derive(q_dec, float(market.liquidity_b))
    return MarketState(
        market_id=int(market.id),
        b=float(market.liquidity_b),
        outcome_ids=[int(o.id) for o in outs],
        outcome_labels=[str(o.label) for o in outs],
        q_dec=q_dec,
        q=q,
        prices=prices,
        status=market.status,
        closes_at=market.closes_at,
    )


class MarketWriter:
    QUEUE_MAXSIZE = 256      # 满则 429（spec § 4.3 第一道背压）
    SUBMIT_TIMEOUT = 10.0    # 等结果超时 → 503（spec § 4.3 第二道背压）

    def __init__(self) -> None:
        self._states: dict[int, MarketState] = {}
        self._queues: dict[int, asyncio.Queue] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._market_by_outcome: dict[int, int] = {}
        self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def get_state(self, market_id: int) -> MarketState | None:
        return self._states.get(market_id)

    def market_id_for_outcome(self, outcome_id: int) -> int | None:
        return self._market_by_outcome.get(outcome_id)

    async def start(self) -> None:
        await self.stop()
        async with async_session_maker() as s:
            markets = (await s.execute(
                select(Market).where(
                    Market.status.in_([MarketStatus.TRADING, MarketStatus.HALT])
                )
            )).scalars().all()
            for m in markets:
                st = await _load_one(s, m)
                self._install(st)
        self._enabled = True
        logger.info("market_writer started: %d markets loaded", len(self._states))

    async def stop(self) -> None:
        self._enabled = False
        for t in self._tasks.values():
            t.cancel()
        for t in list(self._tasks.values()):
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        self._queues.clear()
        self._states.clear()
        self._market_by_outcome.clear()

    async def register_market(self, market_id: int) -> None:
        async with async_session_maker() as s:
            m = await s.get(Market, market_id)
            if m is None:
                return
            st = await _load_one(s, m)
        self._install(st)

    async def reload_state(self, market_id: int) -> None:
        """自愈：从 DB 镜像重读 q / status。失败则标记 unavailable。"""
        try:
            async with async_session_maker() as s:
                m = await s.get(Market, market_id)
                if m is None:
                    raise RuntimeError(f"market {market_id} vanished")
                st_new = await _load_one(s, m)
            st = self._states[market_id]
            st.q_dec, st.q, st.prices = st_new.q_dec, st_new.q, st_new.prices
            st.status, st.closes_at = st_new.status, st_new.closes_at
            st.unavailable = False
            logger.warning("market_writer state reloaded from mirror: market_id=%d", market_id)
        except Exception:
            self._states[market_id].unavailable = True
            logger.critical(
                "market_writer reload FAILED, market %d marked unavailable", market_id,
                exc_info=True,
            )

    def _install(self, st: MarketState) -> None:
        self._states[st.market_id] = st
        for oid in st.outcome_ids:
            self._market_by_outcome[oid] = st.market_id
        if st.market_id not in self._queues:
            self._queues[st.market_id] = asyncio.Queue(maxsize=self.QUEUE_MAXSIZE)
            self._tasks[st.market_id] = asyncio.create_task(
                self._consume(st.market_id), name=f"market-writer-{st.market_id}"
            )

    async def _consume(self, market_id: int) -> None:
        # Task 3 实现完整命令循环；骨架阶段仅挂起等待
        q = self._queues[market_id]
        while True:
            await q.get()


WRITER = MarketWriter()
