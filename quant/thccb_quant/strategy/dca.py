"""DCA 定投策略。spec §6.2。

每 interval_hours 用 quote 估算需要多少 shares 才能花到 cny_per_buy，
然后下买单。total_budget_cny 是该策略总花销上限。
"""
import time
from decimal import Decimal

from thccb_quant.errors import RiskRejected, BusinessError, TransientError
from thccb_quant.strategy.base import Strategy, StrategyContext
from thccb_quant.strategy.registry import register


@register("dca")
class DcaStrategy(Strategy):
    tick_interval_sec = 60  # 主循环每分钟来一次，自己判断要不要下单

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self._outcome_id: int = int(config["outcome_id"])
        self._cny_per_buy = Decimal(str(config["cny_per_buy"]))
        self._interval_sec = float(config["interval_hours"]) * 3600
        self._total_budget = Decimal(str(config["total_budget_cny"]))
        self._spent = Decimal("0")
        self._last_buy_ts: float = 0.0
        self._ctx: StrategyContext | None = None
        self._market_id: int = int(config["market_id"])

    async def setup(self, ctx: StrategyContext) -> None:
        self.market_id = self._market_id
        self._ctx = ctx
        # 从历史 orders 还原 _spent
        rows = await ctx.store.recent_orders(strategy=self.name, limit=10000)
        for r in rows:
            if r["status"] == "success" and r["side"] == "buy":
                self._spent += Decimal(r["cost"])

    async def tick(self) -> None:
        assert self._ctx is not None
        now = time.monotonic()
        if self._last_buy_ts and (now - self._last_buy_ts) < self._interval_sec:
            await self._ctx.store.log_decision(
                strategy=self.name, outcome_id=self._outcome_id,
                action="skip", reason="interval not reached",
            )
            return
        if self._spent + self._cny_per_buy > self._total_budget:
            await self._ctx.store.log_decision(
                strategy=self.name, outcome_id=self._outcome_id,
                action="skip", reason="total budget exhausted",
            )
            return

        # 估算需要多少 shares 才大致花到 cny_per_buy：先 quote 1 share 拿 avg_price
        probe = await self._ctx.rest.quote(
            outcome_id=self._outcome_id, shares=Decimal("1"), side="buy",
        )
        if probe.avg_price <= 0:
            return
        target_shares = (self._cny_per_buy / probe.avg_price).quantize(Decimal("0.000001"))

        try:
            resp = await self._ctx.broker.buy(
                strategy=self.name, outcome_id=self._outcome_id,
                shares=target_shares,
                max_slippage_bps=int(self._ctx.config.get("max_slippage_bps", 300)),
            )
        except (RiskRejected, BusinessError, TransientError) as e:
            await self._ctx.store.log_decision(
                strategy=self.name, outcome_id=self._outcome_id,
                action="skip", reason=f"order failed: {e}",
            )
            return

        self._spent += resp.cost
        self._last_buy_ts = now
        await self._ctx.store.log_decision(
            strategy=self.name, outcome_id=self._outcome_id,
            action="buy", reason="dca tick",
            snapshot={"cost": str(resp.cost), "shares": str(resp.shares),
                      "spent_total": str(self._spent)},
        )
