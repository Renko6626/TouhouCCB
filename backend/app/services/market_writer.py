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

from fastapi import HTTPException
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


@dataclass
class OpOutcome:
    """op 执行结果。op 内部完成 DB 事务；consumer 在 op 返回后统一 apply 内存。"""
    response: Any
    new_q_dec: Optional[list[Decimal]] = None
    new_prices: Optional[list[float]] = None
    new_status: Optional[MarketStatus] = None
    candle_rows: list[dict] = field(default_factory=list)
    publishes: list[tuple[str, dict]] = field(default_factory=list)


class MarketWriter:
    QUEUE_MAXSIZE = 256      # 满则 429（spec § 4.3 第一道背压）
    SUBMIT_TIMEOUT = 10.0    # 等结果超时 → 503（spec § 4.3 第二道背压）

    def __init__(self) -> None:
        self._states: dict[int, MarketState] = {}
        self._queues: dict[int, asyncio.Queue] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._market_by_outcome: dict[int, int] = {}
        self._ops: dict[type, Any] = {}
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
        from app.services.writer_ops import register_all_ops   # 局部 import 避免环
        register_all_ops(self)
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

    def register_op(self, cmd_type: type, op) -> None:
        self._ops[cmd_type] = op

    async def submit(self, cmd) -> Any:
        market_id = cmd.market_id
        q = self._queues.get(market_id)
        st = self._states.get(market_id)
        if q is None or st is None:
            raise HTTPException(status_code=400, detail="市场当前不可交易")
        if st.unavailable:
            raise HTTPException(status_code=503, detail="市场状态异常，暂停服务，请稍后重试")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        try:
            q.put_nowait((cmd, fut))
        except asyncio.QueueFull:
            raise HTTPException(status_code=429, detail="交易过于繁忙，请稍后重试")
        try:
            return await asyncio.wait_for(fut, timeout=self.SUBMIT_TIMEOUT)
        except asyncio.TimeoutError:
            # 命令可能仍在 DB 里执行——结果未知，绝不能说"失败"（spec § 4.3）
            raise HTTPException(status_code=503, detail="服务繁忙，本次操作结果未知，请刷新后确认")

    async def _consume(self, market_id: int) -> None:
        from app.services.realtime import BROKER   # 局部 import 避免环
        q = self._queues[market_id]
        while True:
            cmd, fut = await q.get()
            st = self._states[market_id]
            try:
                if st.unavailable:
                    raise HTTPException(status_code=503, detail="市场状态异常，暂停服务")
                op = self._ops[type(cmd)]
                outcome: OpOutcome = await op(st, cmd)
                # ── commit 已成功（op 返回即视为已 commit）→ apply 内存（spec § 4.4）──
                if outcome.new_q_dec is not None:
                    st.q_dec = outcome.new_q_dec
                    st.q = [float(x) for x in st.q_dec]
                    if outcome.new_prices is not None:
                        st.prices = outcome.new_prices
                    else:
                        _, st.prices = calculate_lmsr_with_prices(st.q, st.b)
                if outcome.new_status is not None:
                    st.status = outcome.new_status
                if outcome.candle_rows:
                    self._merge_candles(outcome.candle_rows)
                for event_type, data in outcome.publishes:
                    await BROKER.publish(market_id, event_type, data)
                if not fut.done():
                    fut.set_result(outcome.response)
            except HTTPException as e:
                # 业务拒绝：op 保证此时事务已回滚 / 未开启，内存零变更
                if not fut.done():
                    fut.set_exception(e)
            except asyncio.CancelledError:
                if not fut.done():
                    fut.set_exception(HTTPException(status_code=503, detail="服务关闭中"))
                raise
            except Exception:
                # 非预期异常：无法区分 commit 前后 → 一律从镜像重读自愈（spec § 4.4）
                logger.critical(
                    "market_writer op crashed, reloading state: market_id=%d cmd=%s",
                    market_id, type(cmd).__name__, exc_info=True,
                )
                if not fut.done():
                    fut.set_exception(HTTPException(
                        status_code=500, detail="交易处理异常，结果未知，请刷新后确认"))
                await self.reload_state(market_id)

    def _merge_candles(self, rows: list[dict]) -> None:
        from app.services.candle_flusher import CANDLE_FLUSHER
        CANDLE_FLUSHER.merge(rows)


WRITER = MarketWriter()
