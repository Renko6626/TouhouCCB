"""VolatilityHarvest 策略。spec docs/superpowers/specs/2026-05-17-volatility-harvest-design.md。

设计语义：
- 在 logit 价格空间维护滑窗中位数 + MAD 作为鲁棒的中心 / 离散度估计
- deadband + tanh 仓位映射：deviation 在阈值内 target == base（不动）；
  过阈值后 excess 进 tanh 平滑反向调整（无 76% 跳变 regression）
- bootstrap：_holding < base 时分散补底仓（节流 + overpriced 跳过 + 单次 cap）
- reconcile：每 reconcile_interval_sec 用真实持仓校准 _holding 防漂移
- trend guard：连续 N 笔同向 trade → 暂停逆势加仓（散户连续追涨时不卖反向）
"""
import math
import statistics
import time
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Optional

from thccb_quant.client.sse import SseEvent
from thccb_quant.errors import BusinessError, RiskRejected, TransientError
from thccb_quant.strategy.base import Strategy, StrategyContext
from thccb_quant.strategy.registry import register


def to_logit(p: float) -> float:
    """价格 → logit。clamp 防边界爆炸。"""
    p = max(0.001, min(0.999, p))
    return math.log(p / (1.0 - p))


def compute_target(
    *, deviation: float, mad: float,
    base: float, max_offset: float,
    k_sigma: float, scale_mad: float,
) -> float:
    """spec §3.2 deadband + tanh 公式。

    threshold = k_sigma * 1.4826 * mad；abs(deviation) ≤ threshold 时 target=base；
    超阈后 u = sign(deviation) * (excess / (scale_mad * mad))，
    target = base - max_offset * tanh(u)。
    deviation>0（涨）→ u>0 → tanh>0 → target<base → 卖；负号必要。
    """
    threshold = k_sigma * 1.4826 * mad
    excess = max(0.0, abs(deviation) - threshold)
    if excess <= 0.0:
        return base
    u = math.copysign(excess / (scale_mad * mad), deviation)
    return base - max_offset * math.tanh(u)


@register("volharvest")
class VolatilityHarvest(Strategy):
    tick_interval_sec = 300  # 不靠 tick，靠 SSE event；偶尔 tick 仅作守护

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        # market routing
        self._market_id = int(config["market_id"])
        self._outcome_id = int(config["outcome_id"])
        # 统计
        self._window_size = int(config["window_size"])
        self._k_sigma = float(config["k_sigma"])
        self._scale_mad = float(config["scale_mad"])
        self._min_mad_logit = float(config["min_mad_logit"])
        self._window: deque[tuple[float, float]] = deque(maxlen=self._window_size)
        # 仓位
        self._base = Decimal(str(config["base_shares"]))
        self._max_offset = Decimal(str(config["max_offset_shares"]))
        self._min_trade = Decimal(str(config["min_trade_shares"]))
        self._max_trade = Decimal(str(config["max_trade_shares"]))
        # bootstrap
        self._boot_step = Decimal(str(config["bootstrap_max_step"]))
        self._boot_interval = float(config["bootstrap_interval_sec"])
        self._boot_skip_overpriced = bool(config["bootstrap_skip_if_overpriced"])
        self._last_boot_ts: float = 0.0
        # reconcile
        self._reconcile_interval = float(config["reconcile_interval_sec"])
        self._reconcile_tol = Decimal(str(config["reconcile_tolerance"]))
        self._last_reconcile_ts: float = 0.0
        # trend guard
        self._trend_n = int(config["trend_guard_events"])
        self._trend_window: deque[str] = deque(maxlen=self._trend_n)
        # 内部状态
        self._holding: Decimal = Decimal("0")
        self._outcome_index: int = -1  # setup 里查 market 后定下
        self._ctx: Optional[StrategyContext] = None

    # ─── lifecycle ────────────────────────────────────────────

    async def setup(self, ctx: StrategyContext) -> None:
        self.market_id = self._market_id  # SseSubscriber 用以路由（必须 setup 里设）
        self._ctx = ctx
        # 从真实持仓 bootstrap _holding（启动期出错就让 trader 启动失败）
        holdings = await ctx.rest.get_holdings()
        actual = next(
            (h.amount for h in holdings if h.outcome_id == self._outcome_id),
            Decimal("0"),
        )
        self._holding = actual
        # 查 market 拿 outcome 排序，定位自己在 market_prices_post 的下标
        market = await ctx.rest.get_market(self._market_id)
        sorted_outcomes = sorted(market.outcomes, key=lambda o: o.id)
        try:
            self._outcome_index = next(
                i for i, o in enumerate(sorted_outcomes)
                if o.id == self._outcome_id
            )
        except StopIteration:
            raise ValueError(
                f"outcome_id {self._outcome_id} not in market {self._market_id}"
            )
        # 启动时设 last_reconcile_ts 以避免第一次 event 就重复 RPC
        self._last_reconcile_ts = time.monotonic()
        ctx.logger.info(
            "volharvest_setup",
            outcome_id=self._outcome_id,
            outcome_index=self._outcome_index,
            holding=str(self._holding),
            base=str(self._base),
            bootstrap_mode=self._holding < self._base,
        )

    async def tick(self) -> None:
        # SSE 驱动；tick 不参与决策
        return

    async def on_sse_event(self, event: SseEvent) -> None:
        assert self._ctx is not None
        if not isinstance(event, SseEvent) or event.type != "trade":
            return
        trade = event.data.get("trade") if isinstance(event.data, dict) else None
        if not trade:
            return
        try:
            trade_outcome_id = int(trade["outcome_id"])
            side = str(trade["type"])
            ts_str = str(trade["timestamp"])
        except (KeyError, TypeError, ValueError):
            self._ctx.logger.warning("volharvest_bad_event",
                                     outcome_id=self._outcome_id)
            return
        # LMSR：全市场任一 outcome 成交都会改变 self 价格，所以不再 early-out
        # 用 market_prices_post[self._outcome_index] 取自己的价
        mp = trade.get("market_prices_post")
        if not isinstance(mp, (list, tuple)) or len(mp) <= self._outcome_index:
            self._ctx.logger.warning(
                "volharvest_bad_event",
                outcome_id=self._outcome_id,
                reason="missing_market_prices_post",
                outcome_index=self._outcome_index,
                mp_len=len(mp) if isinstance(mp, (list, tuple)) else None,
            )
            return
        try:
            price = float(mp[self._outcome_index])
        except (TypeError, ValueError):
            self._ctx.logger.warning(
                "volharvest_bad_event",
                outcome_id=self._outcome_id,
                reason="bad_market_prices_post_value",
            )
            return
        ts_epoch = self._parse_ts(ts_str)
        if ts_epoch is None:
            self._ctx.logger.warning("volharvest_bad_timestamp", ts=ts_str)
            return

        current_logit = to_logit(price)
        self._window.append((ts_epoch, current_logit))
        direction = self._self_price_direction(trade_outcome_id, side)
        self._trend_window.append(direction)

        # Reconcile 先于主流程，让漂移修正影响后续决策
        await self._maybe_reconcile()

        # Bootstrap mode：_holding < base 时优先补底仓，不进主信号
        if self._holding < self._base:
            await self._maybe_bootstrap(price=price, current_logit=current_logit)
            return

        # 主信号流程
        await self._maybe_trade(
            price=price, current_logit=current_logit, ts_epoch=ts_epoch,
        )

    # ─── helpers ──────────────────────────────────────────────

    def _self_price_direction(self, trade_outcome_id: int, side: str) -> str:
        """把 (trade outcome, trade side) 映射成对 self 价格的方向（UP/DOWN）。

        LMSR：买 outcome X → X 的 shares 增加 → X 价上涨、其它 outcome 价下跌。
        所以：self BUY=UP, self SELL=DOWN, other BUY=DOWN, other SELL=UP。
        """
        is_self = (trade_outcome_id == self._outcome_id)
        if side == "BUY":
            return "UP" if is_self else "DOWN"
        # SELL
        return "DOWN" if is_self else "UP"

    @staticmethod
    def _parse_ts(raw: str) -> Optional[float]:
        try:
            s = raw.rstrip("Z")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (ValueError, TypeError):
            return None

    async def _maybe_reconcile(self) -> None:
        assert self._ctx is not None
        now = time.monotonic()
        if now - self._last_reconcile_ts < self._reconcile_interval:
            return
        try:
            holdings = await self._ctx.rest.get_holdings()
        except (BusinessError, TransientError):
            self._ctx.logger.warning("volharvest_reconcile_failed")
            # 不更新 last_reconcile_ts，下次 event 再试
            return
        actual = next(
            (h.amount for h in holdings if h.outcome_id == self._outcome_id),
            Decimal("0"),
        )
        self._last_reconcile_ts = now
        prev = self._holding
        if abs(actual - self._holding) > self._reconcile_tol:
            self._ctx.logger.warning(
                "volharvest_reconcile_drift_corrected",
                actual=str(actual),
                internal=str(prev),
                diff=str(actual - prev),
            )
            self._holding = actual

    async def _maybe_bootstrap(
        self, *, price: float, current_logit: float,
    ) -> None:
        assert self._ctx is not None
        now = time.monotonic()
        if self._last_boot_ts and (now - self._last_boot_ts) < self._boot_interval:
            self._ctx.logger.info(
                "volharvest_bootstrap_skip",
                reason="throttled", holding=str(self._holding),
            )
            return
        # overpriced 检查：window 已热 + deviation > 0 → skip
        if self._boot_skip_overpriced and len(self._window) >= self._window_size // 2:
            logits = [lg for _, lg in self._window]
            median_logit = statistics.median(logits)
            deviation = current_logit - median_logit
            if deviation > 0:
                self._ctx.logger.info(
                    "volharvest_bootstrap_skip_overpriced",
                    price=str(price), deviation=deviation,
                    holding=str(self._holding),
                )
                return
        # 算补仓量
        need = self._base - self._holding
        step_raw = min(need, self._boot_step, self._max_trade)
        step = step_raw if step_raw > 0 else Decimal("0")
        # 量化到整数 shares（反侦察：散户都是整数下单）
        step = step.quantize(Decimal("1"), rounding=ROUND_DOWN)
        if step < self._min_trade.quantize(Decimal("1"), rounding=ROUND_DOWN):
            self._ctx.logger.info(
                "volharvest_bootstrap_skip",
                reason="step_below_min", step=str(step),
                holding=str(self._holding),
            )
            return
        try:
            resp = await self._ctx.broker.buy(
                strategy=self.name,
                outcome_id=self._outcome_id,
                shares=step,
                max_slippage_bps=int(self._ctx.config["max_slippage_bps"]),
            )
        except (RiskRejected, BusinessError, TransientError) as e:
            await self._ctx.store.log_decision(
                strategy=self.name, outcome_id=self._outcome_id,
                action="skip", reason=f"bootstrap buy failed: {e}",
                snapshot={"step": str(step), "holding": str(self._holding)},
            )
            return
        self._holding += resp.shares
        self._last_boot_ts = now
        self._ctx.logger.info(
            "volharvest_trade",
            side="buy", shares=str(resp.shares), cost=str(resp.cost),
            bootstrap_mode=True, holding_after=str(self._holding),
        )
        await self._ctx.store.log_decision(
            strategy=self.name, outcome_id=self._outcome_id,
            action="buy", reason="bootstrap",
            snapshot={"step": str(step), "holding_after": str(self._holding)},
        )

    async def _maybe_trade(
        self, *, price: float, current_logit: float, ts_epoch: float,
    ) -> None:
        assert self._ctx is not None
        # 预热检查
        needed = max(self._window_size // 2, 1)
        if len(self._window) < needed:
            self._ctx.logger.info(
                "volharvest_window_not_warm",
                window_len=len(self._window), needed=needed,
            )
            return
        # 统计
        logits = [lg for _, lg in self._window]
        median_logit = statistics.median(logits)
        mad_logit = statistics.median(abs(lg - median_logit) for lg in logits)
        if mad_logit < self._min_mad_logit:
            self._ctx.logger.info(
                "volharvest_mad_too_small", mad=mad_logit,
            )
            return
        deviation = current_logit - median_logit
        threshold = self._k_sigma * 1.4826 * mad_logit
        excess = max(0.0, abs(deviation) - threshold)
        raw_target_f = compute_target(
            deviation=deviation, mad=mad_logit,
            base=float(self._base), max_offset=float(self._max_offset),
            k_sigma=self._k_sigma, scale_mad=self._scale_mad,
        )
        raw_target = Decimal(str(raw_target_f))
        delta = raw_target - self._holding

        win_age = ts_epoch - self._window[0][0]

        # 量化到整数 shares（反侦察：散户都是整数下单）
        shares_abs = abs(delta).quantize(Decimal("1"), rounding=ROUND_DOWN)

        # min trade 过滤（含 deadband 内 delta==0 的情况）
        if shares_abs < self._min_trade:
            self._ctx.logger.info(
                "volharvest_signal",
                price=str(price), logit_price=current_logit,
                median_logit=median_logit, mad=mad_logit,
                threshold=threshold, deviation=deviation,
                excess=excess, raw_target=str(raw_target),
                clipped_target=str(raw_target), delta=str(delta),
                side="hold",
                reason="deadband" if excess == 0.0 else "min_trade",
                current_holding=str(self._holding),
                window_len=len(self._window),
                window_age_seconds=win_age,
                bootstrap_mode=False,
            )
            return

        clip_reason = "ok"
        max_trade_int = self._max_trade.quantize(Decimal("1"), rounding=ROUND_DOWN)
        if shares_abs > max_trade_int:
            shares_abs = max_trade_int
            clip_reason = "clipped"
        delta = shares_abs if delta > 0 else -shares_abs

        # trend guard：连续 N 笔同向 → 拦逆势加仓
        if (
            len(self._trend_window) == self._trend_n
            and len(set(self._trend_window)) == 1
        ):
            guard_side = self._trend_window[0]
            if guard_side == "UP" and delta < 0:
                self._ctx.logger.info(
                    "volharvest_trend_guard_blocked",
                    guard_side="UP", blocked_action="sell",
                    last_n_sides=list(self._trend_window),
                )
                await self._ctx.store.log_decision(
                    strategy=self.name, outcome_id=self._outcome_id,
                    action="skip", reason="trend_guard_up_blocks_sell",
                    snapshot={"delta": str(delta), "deviation": deviation},
                )
                return
            if guard_side == "DOWN" and delta > 0:
                self._ctx.logger.info(
                    "volharvest_trend_guard_blocked",
                    guard_side="DOWN", blocked_action="buy",
                    last_n_sides=list(self._trend_window),
                )
                await self._ctx.store.log_decision(
                    strategy=self.name, outcome_id=self._outcome_id,
                    action="skip", reason="trend_guard_down_blocks_buy",
                    snapshot={"delta": str(delta), "deviation": deviation},
                )
                return

        # 下单
        side = "buy" if delta > 0 else "sell"
        shares = abs(delta)
        try:
            if side == "buy":
                resp = await self._ctx.broker.buy(
                    strategy=self.name,
                    outcome_id=self._outcome_id,
                    shares=shares,
                    max_slippage_bps=int(self._ctx.config["max_slippage_bps"]),
                )
                self._holding += resp.shares
            else:
                resp = await self._ctx.broker.sell(
                    strategy=self.name,
                    outcome_id=self._outcome_id,
                    shares=shares,
                    max_slippage_bps=int(self._ctx.config["max_slippage_bps"]),
                )
                self._holding -= resp.shares
        except (RiskRejected, BusinessError, TransientError) as e:
            await self._ctx.store.log_decision(
                strategy=self.name, outcome_id=self._outcome_id,
                action="skip", reason=f"{side} failed: {e}",
                snapshot={"delta": str(delta), "deviation": deviation},
            )
            return

        self._ctx.logger.info(
            "volharvest_signal",
            price=str(price), logit_price=current_logit,
            median_logit=median_logit, mad=mad_logit,
            threshold=threshold, deviation=deviation,
            excess=excess, raw_target=str(raw_target),
            clipped_target=str(raw_target), delta=str(delta),
            side=side, reason=clip_reason,
            current_holding=str(self._holding),
            window_len=len(self._window),
            window_age_seconds=win_age,
            bootstrap_mode=False,
        )
        self._ctx.logger.info(
            "volharvest_trade",
            side=side, shares=str(resp.shares), cost=str(resp.cost),
            bootstrap_mode=False, holding_after=str(self._holding),
        )
        await self._ctx.store.log_decision(
            strategy=self.name, outcome_id=self._outcome_id,
            action=side, reason=f"signal {clip_reason}",
            snapshot={
                "deviation": deviation, "delta": str(delta),
                "holding_after": str(self._holding),
            },
        )
