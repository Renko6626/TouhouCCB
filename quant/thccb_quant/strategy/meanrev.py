"""MeanRev 策略。spec docs/superpowers/specs/2026-05-18-meanrev-strategy-design.md。

设计语义：
- 二元市场（恰好 2 outcomes）logit + EMA 跟踪 outcome A 的中心价
- 每个 SSE trade event 判一次：偏离阈值 → 优先卖对面持仓，否则买被低估的
- size = floor(cash * trade_pct / target_price)，量化整数
- 启动从 rest.get_holdings + get_user_summary bootstrap 内部 state
- EMA 状态重启不持久化（重新预热）—— 文档化的取舍
"""
import math
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


@register("meanrev")
class MeanRevStrategy(Strategy):
    tick_interval_sec = 300  # SSE 驱动；tick 仅作守护 no-op

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self._market_id = int(config["market_id"])
        self._ema_alpha = float(config["ema_alpha"])
        self._threshold_logit = float(config["threshold_logit"])
        self._warmup_events = int(config["warmup_events"])
        self._trade_pct = Decimal(str(config["trade_pct_of_cash"]))
        self._min_trade = Decimal(str(config["min_trade"]))
        self._max_holding_per_side = Decimal(str(config["max_holding_per_side"]))
        # 偏离越大下单越大；cap=1 退化为固定 size；典型 2-5 之间
        self._size_scale_cap = float(config["size_scale_cap"])

        if not (0.0 < self._ema_alpha <= 1.0):
            raise ValueError("ema_alpha must be in (0, 1]")
        if self._threshold_logit <= 0:
            raise ValueError("threshold_logit must be > 0")
        if self._trade_pct <= Decimal("0") or self._trade_pct > Decimal("1"):
            raise ValueError("trade_pct_of_cash must be in (0, 1]")
        if self._min_trade <= Decimal("0"):
            raise ValueError("min_trade must be > 0")
        if self._max_holding_per_side <= Decimal("0"):
            raise ValueError("max_holding_per_side must be > 0")
        if self._size_scale_cap < 1.0:
            raise ValueError("size_scale_cap must be >= 1.0")

        # State
        self._ema_logit: Optional[float] = None
        self._event_count: int = 0
        self._holding: dict[int, Decimal] = {}
        self._cash: Decimal = Decimal("0")
        self._outcome_ids: tuple[int, int] = (0, 0)  # (id_A, id_B) by sorted asc
        self._ctx: Optional[StrategyContext] = None

    # ─── lifecycle ────────────────────────────────────────────

    async def setup(self, ctx: StrategyContext) -> None:
        self.market_id = self._market_id  # SseSubscriber routing
        self._ctx = ctx

        market = await ctx.rest.get_market(self._market_id)
        sorted_outcomes = sorted(market.outcomes, key=lambda o: o.id)
        if len(sorted_outcomes) != 2:
            raise ValueError(
                f"meanrev requires binary market (N=2), "
                f"market {self._market_id} has {len(sorted_outcomes)} outcomes"
            )
        id_a = int(sorted_outcomes[0].id)
        id_b = int(sorted_outcomes[1].id)
        self._outcome_ids = (id_a, id_b)

        holdings = await ctx.rest.get_holdings()
        self._holding = {id_a: Decimal("0"), id_b: Decimal("0")}
        for h in holdings:
            if h.outcome_id in self._holding:
                self._holding[h.outcome_id] = h.amount

        summary = await ctx.rest.get_user_summary()
        self._cash = summary.cash

        ctx.logger.info(
            "meanrev_setup",
            market_id=self._market_id,
            outcome_a=id_a, outcome_b=id_b,
            holding_a=str(self._holding[id_a]),
            holding_b=str(self._holding[id_b]),
            cash=str(self._cash),
            ema_alpha=self._ema_alpha,
            threshold_logit=self._threshold_logit,
            warmup_events=self._warmup_events,
        )

    async def tick(self) -> None:
        return  # SSE-driven; tick is no-op

    async def on_sse_event(self, event: SseEvent) -> None:
        assert self._ctx is not None
        if not isinstance(event, SseEvent) or event.type != "trade":
            return
        trade = event.data.get("trade") if isinstance(event.data, dict) else None
        if not trade:
            return
        mp = trade.get("market_prices_post")
        if not isinstance(mp, (list, tuple)) or len(mp) < 2:
            self._ctx.logger.warning(
                "meanrev_bad_event", reason="missing_market_prices_post",
            )
            return
        try:
            price_a = float(mp[0])
        except (TypeError, ValueError):
            self._ctx.logger.warning(
                "meanrev_bad_event", reason="bad_market_prices_post_value",
            )
            return

        current_logit = to_logit(price_a)
        if self._ema_logit is None:
            self._ema_logit = current_logit
        else:
            a = self._ema_alpha
            self._ema_logit = a * current_logit + (1.0 - a) * self._ema_logit
        self._event_count += 1

        if self._event_count < self._warmup_events:
            return
        if self._event_count == self._warmup_events:
            self._ctx.logger.info(
                "meanrev_warmup_done",
                event_count=self._event_count,
                ema_logit=self._ema_logit,
                price_a=price_a,
            )

        deviation = current_logit - self._ema_logit
        # size_multiplier：|deviation| 越大下单越激进，
        # 在 threshold 处 = 1（基础 size），饱和于 size_scale_cap。
        size_multiplier = min(
            abs(deviation) / self._threshold_logit if self._threshold_logit > 0 else 0.0,
            self._size_scale_cap,
        )
        # 信号诊断行：每个 post-warmup event 都吐一条，无论是否动作，
        # 让 dry-run / 复盘能 jq 'select(.event=="meanrev_signal")' 看清策略视角
        self._ctx.logger.info(
            "meanrev_signal",
            price_a=price_a,
            logit_price=current_logit,
            ema_logit=self._ema_logit,
            deviation=deviation,
            threshold=self._threshold_logit,
            in_deadband=abs(deviation) < self._threshold_logit,
            size_multiplier=size_multiplier,
            holding_a=str(self._holding[self._outcome_ids[0]]),
            holding_b=str(self._holding[self._outcome_ids[1]]),
            cash=str(self._cash),
        )
        if abs(deviation) < self._threshold_logit:
            return  # deadband

        id_a, id_b = self._outcome_ids
        if deviation > 0:
            sell_id, sell_price = id_a, price_a
            buy_id, buy_price = id_b, 1.0 - price_a
        else:
            sell_id, sell_price = id_b, 1.0 - price_a
            buy_id, buy_price = id_a, price_a

        target_amount = (
            self._cash * self._trade_pct * Decimal(str(size_multiplier))
        ).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        if target_amount <= Decimal("0"):
            await self._log_skip("no_cash", price_a, deviation)
            return

        sell_shares = self._compute_sell_shares(
            target_amount=target_amount, side_price=sell_price, side_id=sell_id,
        )
        if sell_shares >= self._min_trade:
            await self._do_sell(
                outcome_id=sell_id, shares=sell_shares,
                price_a=price_a, deviation=deviation,
            )
            return

        buy_shares = self._compute_buy_shares(
            target_amount=target_amount, side_price=buy_price, side_id=buy_id,
        )
        if buy_shares >= self._min_trade:
            await self._do_buy(
                outcome_id=buy_id, shares=buy_shares,
                price_a=price_a, deviation=deviation,
            )
            return

        await self._log_skip(
            "size_below_min", price_a, deviation,
            sell_shares=str(sell_shares), buy_shares=str(buy_shares),
        )

    # ─── helpers ──────────────────────────────────────────────

    def _compute_sell_shares(
        self, *, target_amount: Decimal, side_price: float, side_id: int,
    ) -> Decimal:
        """卖出数量：min(target_amount / price, holding)；末尾整体取整向下
        （holding 可能因别处遗留小数，如旧策略残余的 7.72，单独 floor raw 不够）。"""
        if side_price <= 0:
            return Decimal("0")
        raw = target_amount / Decimal(str(side_price))
        available = min(self._holding[side_id], raw)
        return available.quantize(Decimal("1"), rounding=ROUND_DOWN)

    def _compute_buy_shares(
        self, *, target_amount: Decimal, side_price: float, side_id: int,
    ) -> Decimal:
        """买入数量：min(target_amount / price, max_holding_per_side - holding)；末尾整体取整向下。"""
        if side_price <= 0:
            return Decimal("0")
        cap_remain = self._max_holding_per_side - self._holding[side_id]
        if cap_remain <= Decimal("0"):
            return Decimal("0")
        raw = target_amount / Decimal(str(side_price))
        desired = min(raw, cap_remain)
        return desired.quantize(Decimal("1"), rounding=ROUND_DOWN)

    async def _do_buy(
        self, *, outcome_id: int, shares: Decimal,
        price_a: float, deviation: float,
    ) -> None:
        assert self._ctx is not None
        try:
            resp = await self._ctx.broker.buy(
                strategy=self.name, outcome_id=outcome_id, shares=shares,
                max_slippage_bps=int(self._ctx.config["max_slippage_bps"]),
            )
        except (RiskRejected, BusinessError, TransientError) as e:
            await self._log_skip(
                f"buy_failed: {e}", price_a, deviation,
                outcome_id=outcome_id, shares=str(shares),
            )
            return
        self._holding[outcome_id] += resp.shares
        # 用 resp.cost 维护 cash，不用 resp.new_cash —— 因为 DryRunBroker 硬返回
        # new_cash=0（dryrun 不模拟现金），用 cost 增量在 live + dry-run 都正确：
        # BUY: resp.cost > 0 （付出），cash 减；SELL: resp.cost < 0（收回），cash 加。
        self._cash -= resp.cost
        self._ctx.logger.info(
            "meanrev_trade", side="buy", outcome_id=outcome_id,
            shares=str(resp.shares), cost=str(resp.cost),
            cash_after=str(self._cash),
            price_a=price_a, deviation=deviation,
            holding_a=str(self._holding[self._outcome_ids[0]]),
            holding_b=str(self._holding[self._outcome_ids[1]]),
        )
        await self._ctx.store.log_decision(
            strategy=self.name, outcome_id=outcome_id,
            action="buy", reason="signal_buy",
            snapshot=self._snapshot(price_a, deviation,
                                    outcome_id=outcome_id,
                                    shares=str(resp.shares),
                                    cost=str(resp.cost)),
        )

    async def _do_sell(
        self, *, outcome_id: int, shares: Decimal,
        price_a: float, deviation: float,
    ) -> None:
        assert self._ctx is not None
        try:
            resp = await self._ctx.broker.sell(
                strategy=self.name, outcome_id=outcome_id, shares=shares,
                max_slippage_bps=int(self._ctx.config["max_slippage_bps"]),
            )
        except (RiskRejected, BusinessError, TransientError) as e:
            await self._log_skip(
                f"sell_failed: {e}", price_a, deviation,
                outcome_id=outcome_id, shares=str(shares),
            )
            return
        self._holding[outcome_id] -= resp.shares
        # 见 _do_buy 同处注释：cash -= cost 在 BUY/SELL 都正确
        self._cash -= resp.cost
        self._ctx.logger.info(
            "meanrev_trade", side="sell", outcome_id=outcome_id,
            shares=str(resp.shares), cost=str(resp.cost),
            cash_after=str(self._cash),
            price_a=price_a, deviation=deviation,
            holding_a=str(self._holding[self._outcome_ids[0]]),
            holding_b=str(self._holding[self._outcome_ids[1]]),
        )
        await self._ctx.store.log_decision(
            strategy=self.name, outcome_id=outcome_id,
            action="sell", reason="signal_sell",
            snapshot=self._snapshot(price_a, deviation,
                                    outcome_id=outcome_id,
                                    shares=str(resp.shares),
                                    cost=str(resp.cost)),
        )

    async def _log_skip(
        self, reason: str, price_a: float, deviation: float, **extras,
    ) -> None:
        assert self._ctx is not None
        outcome_id_for_log = int(extras.get("outcome_id", self._outcome_ids[0]))
        await self._ctx.store.log_decision(
            strategy=self.name, outcome_id=outcome_id_for_log,
            action="skip", reason=reason,
            snapshot=self._snapshot(price_a, deviation, **extras),
        )

    def _snapshot(self, price_a: float, deviation: float, **extras) -> dict:
        return {
            "price_a": price_a,
            "deviation": deviation,
            "ema_logit": self._ema_logit,
            "event_count": self._event_count,
            "holding_a": str(self._holding[self._outcome_ids[0]]),
            "holding_b": str(self._holding[self._outcome_ids[1]]),
            "cash": str(self._cash),
            **extras,
        }
