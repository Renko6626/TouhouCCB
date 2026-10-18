"""单写者内存状态机（spec § 4）。

每个市场一条 asyncio.Queue + 常驻 consumer task；内存 q 是权威，
DB outcome.total_shares 是镜像（每笔 commit 内同步写、值恒等，
spec § 4.4 的 6dp 不动点）。有经济写所有权的实例在启动时运行 writer；只读实例不运行。
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
from app.services.history_ring import RING_SPEC, HistoryRing
from app.services.lmsr import calculate_lmsr_with_prices, quantize_cost, quantize_price
from app.services import tick_broadcaster as _tick

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
    unavailable: bool = False     # 自愈失败后置 True：该市场一律 503（spec § 4.4 异常策略）
    rings: dict[int, "HistoryRing"] = field(default_factory=dict)  # outcome_id → 环形缓冲（spec § 7.1）
    # 已合并进 ring 的最后一笔成交 id（单写者 → 单调递增）。snapshot 随 history_tail 下发，
    # 前端对 tick 帧里 id ≤ 它的成交跳过 K 线 applyTrade——subscribe 取 anchor 与读 ring
    # 之间完成的成交会同时出现在尾巴和队列里，否则 forming 桶 volume 翻倍（审计 M4）
    last_ring_trade_id: int = 0


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
    # ── ring 回灌（spec § 7.1）：从镜像 OutcomeCandle 读回各档窗口内的桶 ──
    # 依赖 lifespan 顺序：_resync_recent_candles 先于 WRITER.start() 执行，
    # 崩溃丢失的 ≤5s candle 已被重放修复，镜像此刻可信。
    import time as _time
    from app.models.base import OutcomeCandle
    now_epoch = int(_time.time())
    max_window = max(t.window for t in RING_SPEC.values())
    from datetime import datetime as _dt, timezone as _tz
    cutoff = _dt.fromtimestamp(now_epoch - max_window, tz=_tz.utc)
    rings: dict[int, HistoryRing] = {int(o.id): HistoryRing() for o in outs}
    candle_rows = (await session.execute(
        select(OutcomeCandle).where(
            OutcomeCandle.outcome_id.in_([int(o.id) for o in outs]),
            OutcomeCandle.interval.in_(list(RING_SPEC.keys())),
            OutcomeCandle.bucket_start >= cutoff,
        )
    )).scalars().all()
    for c in candle_rows:
        bs = c.bucket_start if c.bucket_start.tzinfo else c.bucket_start.replace(tzinfo=_tz.utc)
        tier = RING_SPEC[c.interval]
        if int(bs.timestamp()) < now_epoch - tier.window:
            continue   # 该档窗口外（cutoff 用的是最长窗口 90d，短档要再过滤）
        rings[int(c.outcome_id)].merge_row({
            "outcome_id": int(c.outcome_id), "interval": c.interval,
            "bucket_start": bs,
            "open_price": c.open_price, "high_price": c.high_price,
            "low_price": c.low_price, "close_price": c.close_price,
            "volume_shares": c.volume_shares, "n_trades": c.n_trades,
            "updated_at": c.updated_at,
        })
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
        rings=rings,
    )


@dataclass
class OpOutcome:
    """op 执行结果。op 内部完成 DB 事务；consumer 在 op 返回后统一 apply 内存。"""
    response: Any
    new_q_dec: Optional[list[Decimal]] = None
    new_status: Optional[MarketStatus] = None
    candle_rows: list[dict] = field(default_factory=list)
    tick_trade: Optional[dict] = None        # 本笔成交 payload，投喂 tick 帧
    tick_settlement: Optional[dict] = None   # 仅 resolve：{"winning_outcome_id", "settled_at"}
    account_changes: Optional[list[tuple[int, int]]] = None


@dataclass(frozen=True)
class _ReloadCmd:
    market_id: int


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
        self._bootstrap_lock = asyncio.Lock()

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
        for market_id in self._states:
            self._invalidate_read_state(market_id)
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

    @staticmethod
    def _invalidate_read_state(market_id: int) -> None:
        from app.schemas.trading_context import GroupKey
        from app.services.trading_prices import REGISTRY
        REGISTRY.invalidate(GroupKey(product="lmsr", id=market_id))

    def _install_read_state(self, st: MarketState) -> None:
        from app.services.market_read_state import LmsrReadState
        from app.services.trading_prices import REGISTRY
        try:
            REGISTRY.install_committed(LmsrReadState(
                id=st.market_id, b=st.b, outcome_ids=tuple(st.outcome_ids),
                q_dec=tuple(st.q_dec), status=st.status, closes_at=st.closes_at,
            ))
        except BaseException:
            self._invalidate_read_state(st.market_id)
            logger.exception(
                "LMSR database committed; snapshot installation failed: market_id=%d",
                st.market_id,
            )
            raise

    async def register_market(self, market_id: int) -> None:
        if market_id in self._queues:
            await self.reload_state(market_id)
            return
        # New IDs bootstrap once; existing IDs reload through their consumer.
        async with self._bootstrap_lock:
            if market_id not in self._queues:
                self._invalidate_read_state(market_id)
                try:
                    async with async_session_maker() as s:
                        m = await s.get(Market, market_id)
                        if m is None:
                            return
                        st = await _load_one(s, m)
                    self._install(st)
                except BaseException:
                    self._invalidate_read_state(market_id)
                    raise
                return
        await self.reload_state(market_id)

    async def reload_state(self, market_id: int) -> None:
        """Serialize external reloads with every command for this market."""
        await self.submit(_ReloadCmd(market_id))

    async def _reload_state(self, market_id: int) -> None:
        """Reload the whole committed pricing source; failure discards old reads."""
        self._invalidate_read_state(market_id)
        existing = self._states.get(market_id)
        if existing is not None:
            existing.unavailable = True
        try:
            # Flush before rebuilding rings so pending candles are preserved.
            from app.services.candle_flusher import CANDLE_FLUSHER
            if await CANDLE_FLUSHER.flush_once() == 0 and CANDLE_FLUSHER._pending:
                logger.error("reload_state: candle flush failed, ring may miss pending rows")
            async with async_session_maker() as s:
                m = await s.get(Market, market_id)
                if m is None:
                    raise RuntimeError(f"market {market_id} vanished")
                st_new = await _load_one(s, m)
            st = self._states[market_id]
            # Prices were derived with st_new.b: keep that exact source and
            # outcome ordering, rather than pairing new q with old metadata.
            for oid in st.outcome_ids:
                self._market_by_outcome.pop(oid, None)
            st.b = st_new.b
            st.outcome_ids, st.outcome_labels = st_new.outcome_ids, st_new.outcome_labels
            st.q_dec, st.q, st.prices = st_new.q_dec, st_new.q, st_new.prices
            st.status, st.closes_at = st_new.status, st_new.closes_at
            st.rings = st_new.rings
            for oid in st.outcome_ids:
                self._market_by_outcome[oid] = market_id
            self._install_read_state(st)
            st.unavailable = False
            logger.warning("market_writer state reloaded from mirror: market_id=%d", market_id)
        except Exception:
            # stop() may already have cleared _states; do not kill consumer.
            st = self._states.get(market_id)
            if st is not None:
                st.unavailable = True
            self._invalidate_read_state(market_id)
            logger.critical(
                "market_writer reload FAILED, market %d marked unavailable", market_id,
                exc_info=True,
            )

    def _install(self, st: MarketState) -> None:
        self._install_read_state(st)
        previous = self._states.get(st.market_id)
        if previous is not None:
            for oid in previous.outcome_ids:
                self._market_by_outcome.pop(oid, None)
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
        if st.unavailable and not isinstance(cmd, _ReloadCmd):
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
        q = self._queues[market_id]
        while True:
            cmd, fut = await q.get()
            if fut.done():
                # 调用方已超时/放弃（submit 的 wait_for cancel 了 future）——不再执行。
                # 否则 DB 卡顿期积压的命令会在几十秒后按完全不同的价格无保护成交
                # （accept_any_slippage 的平仓单没有任何护栏，final review IMP-1）。
                continue
            st = self._states[market_id]
            try:
                if isinstance(cmd, _ReloadCmd):
                    # Maintenance must remain available when economic writes are
                    # blocked. Recovery calls the same private loader directly.
                    await self._reload_state(market_id)
                    if not fut.done():
                        fut.set_result(None)
                    continue
                if st.unavailable:
                    raise HTTPException(status_code=503, detail="市场状态异常，暂停服务")
                op = self._ops[type(cmd)]
                outcome: OpOutcome = await self._execute_command(st, cmd, op)
                if outcome.account_changes is not None:
                    from app.services.account_events import publish_accounts
                    publish_accounts(outcome.account_changes, "lmsr")
                # ── tick 帧投喂（spec § 5.1）──
                prices_8dp = [float(quantize_price(p)) for p in st.prices]
                if outcome.tick_trade is not None:
                    _tick.TICK_BROADCASTER.feed_trade(
                        market_id, prices_8dp, outcome.tick_trade, st.status)
                elif outcome.new_status is not None:
                    _tick.TICK_BROADCASTER.feed_status(
                        market_id, st.status, outcome.tick_settlement, prices=prices_8dp)
                elif outcome.new_q_dec is not None:
                    # 强平：q 变了但没有成交事件 → 空 trades 帧推新价格
                    _tick.TICK_BROADCASTER.feed_prices(market_id, prices_8dp, st.status)
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
                await self._reload_state(market_id)

    async def _execute_command(self, st, cmd, op):
        from app.services.credit.ownership import OWNERSHIP
        OWNERSHIP.require_writes()
        try:
            outcome = await op(st, cmd)
            self._apply_outcome(st, outcome)
            return outcome
        except HTTPException:
            raise
        except BaseException:
            st.unavailable = True
            self._invalidate_read_state(st.market_id)
            raise

    def _apply_outcome(self, st, outcome):
        # ── commit 已成功（op 返回即视为已 commit）→ apply 内存（spec § 4.4）──
        if outcome.new_q_dec is not None:
            st.q_dec = outcome.new_q_dec
            st.q = [float(x) for x in st.q_dec]
            # MIN-1：prices 恒从量化后的 q 重新导出，保证 prices == f(q)。
            # 阶段 2 起 prices 进 tick 帧，若沿用 op 浮点直加算出的 new_prices，
            # 重启/自愈从镜像重读后帧价格会出现末位跳变
            _, st.prices = calculate_lmsr_with_prices(st.q, st.b)
        if outcome.new_status is not None:
            st.status = outcome.new_status
        if outcome.candle_rows:
            self._merge_candles(outcome.candle_rows)
            # ring 与 flusher 吃同一份行——两者永远一致（spec § 7.5）
            for row in outcome.candle_rows:
                ring = st.rings.get(int(row["outcome_id"]))
                if ring is not None:
                    ring.merge_row(row)
            if outcome.tick_trade is not None:
                st.last_ring_trade_id = int(outcome.tick_trade.get("id") or 0)

        # The consumer publishes account hints only after this complete install.
        self._install_read_state(st)

    def _merge_candles(self, rows: list[dict]) -> None:
        from app.services.candle_flusher import CANDLE_FLUSHER
        CANDLE_FLUSHER.merge(rows)


WRITER = MarketWriter()
