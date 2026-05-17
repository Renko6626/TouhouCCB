"""网格策略。spec §6.2。

把 [price_low, price_high] 区间均分为 grid_count 段，得到 grid_count+1 个格点。
每个格点 i 代表「该价位上是否持有一份 shares_per_grid 的多头」（_held[i]）。

设计语义（解决「价格不变不应反复成交」的歧义）：
- 每次 tick 记录当前 price 所在的 bin（即落在哪两个相邻格点之间），与上一次 tick
  的 _last_bin 比较；仅在 bin 发生变化（或首次 setup 后的第一次 tick）时才考虑下单，
  避免同价位反复触发。
- 首次 tick：在当前价上方最近的未持仓格点买入一份。
- bin 下移（价格跌）：在当前价上方最近的未持仓格点买入一份。
- bin 上移（价格涨）：在当前价下方最近的已持仓格点卖出一份。
- price 越过 [price_low, price_high] 区间 → 不动作。

每次 tick 最多一笔买 + 一笔卖；状态从 orders 表 replay 恢复。
"""
from decimal import Decimal
from typing import Optional

from thccb_quant.errors import BusinessError, RiskRejected, TransientError
from thccb_quant.strategy.base import Strategy, StrategyContext
from thccb_quant.strategy.registry import register


@register("grid")
class GridStrategy(Strategy):
    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self._market_id = int(config["market_id"])
        self._outcome_id = int(config["outcome_id"])
        self._low = float(config["price_low"])
        self._high = float(config["price_high"])
        self._count = int(config["grid_count"])
        self._shares_per_grid = Decimal(str(config["shares_per_grid"]))
        self.tick_interval_sec = int(config.get("tick_interval_sec", 30))

        if self._count <= 0:
            raise ValueError("grid_count must be > 0")
        if self._high <= self._low:
            raise ValueError("price_high must be > price_low")

        step = (self._high - self._low) / self._count
        # 格点列表，长度 = grid_count + 1
        self._grids: list[float] = [self._low + i * step for i in range(self._count + 1)]
        self._held: list[bool] = [False] * len(self._grids)
        self._last_bin: Optional[int] = None
        self._ctx: Optional[StrategyContext] = None

    # ─── lifecycle ─────────────────────────────────────────────────

    async def setup(self, ctx: StrategyContext) -> None:
        self.market_id = self._market_id
        self._ctx = ctx
        # 从历史 orders 表按时间顺序 replay 出 _held 状态
        rows = await ctx.store.recent_orders(strategy=self.name, limit=10000)
        # recent_orders 返回的是 id DESC（最新在前），按时间正序需要反转
        for r in reversed(rows):
            if r["status"] != "success" or int(r["outcome_id"]) != self._outcome_id:
                continue
            price = float(r["price"]) if r["price"] else None
            if price is None:
                continue
            idx = self._nearest_grid(price)
            if r["side"] == "buy":
                self._held[idx] = True
            elif r["side"] == "sell":
                self._held[idx] = False

    # ─── helpers ───────────────────────────────────────────────────

    def _nearest_grid(self, price: float) -> int:
        return min(range(len(self._grids)), key=lambda i: abs(self._grids[i] - price))

    def _bin_of(self, price: float) -> Optional[int]:
        """返回 price 所在的 bin index i（满足 grids[i] <= price < grids[i+1]）。
        若 price 超出 [low, high] 区间则返回 None。"""
        if price < self._low or price >= self._high:
            return None
        for i in range(len(self._grids) - 1):
            if self._grids[i] <= price < self._grids[i + 1]:
                return i
        return None

    def _lowest_unheld_above(self, bin_idx: int) -> Optional[int]:
        """当前 bin 上方（含 bin+1）最近的未持仓格点。"""
        for j in range(bin_idx + 1, len(self._grids)):
            if not self._held[j]:
                return j
        return None

    def _highest_held_at_or_below(self, bin_idx: int) -> Optional[int]:
        """当前 bin 下方（含 bin）最近的已持仓格点。"""
        for j in range(bin_idx, -1, -1):
            if self._held[j]:
                return j
        return None

    # ─── tick ──────────────────────────────────────────────────────

    async def tick(self) -> None:
        assert self._ctx is not None
        ctx = self._ctx

        market = await ctx.rest.get_market(self._market_id)
        outcome = next((o for o in market.outcomes if o.id == self._outcome_id), None)
        if outcome is None:
            await ctx.store.log_decision(
                strategy=self.name, outcome_id=self._outcome_id,
                action="skip", reason="outcome not found",
            )
            return
        price = float(outcome.current_price)
        bin_idx = self._bin_of(price)

        if bin_idx is None:
            await ctx.store.log_decision(
                strategy=self.name, outcome_id=self._outcome_id,
                action="skip", reason=f"price {price} outside grid [{self._low}, {self._high})",
                snapshot={"price": str(price)},
            )
            # 注意：不更新 _last_bin，下次回到区间内仍按「首次进入」处理
            return

        first_tick = self._last_bin is None
        bin_changed = first_tick or bin_idx != self._last_bin

        if not bin_changed:
            await ctx.store.log_decision(
                strategy=self.name, outcome_id=self._outcome_id,
                action="skip", reason=f"same bin {bin_idx}",
                snapshot={"price": str(price), "bin": bin_idx},
            )
            return

        bin_direction = (
            "init" if first_tick
            else ("down" if bin_idx < (self._last_bin or 0) else "up")
        )

        # 价格下行 or 首次 → 在上方未持仓格点买入
        if first_tick or bin_idx < (self._last_bin or 0):
            buy_idx = self._lowest_unheld_above(bin_idx)
            if buy_idx is not None:
                await self._try_buy(
                    grid_idx=buy_idx, current_price=price, direction=bin_direction,
                )

        # 价格上行 → 在下方已持仓格点卖出
        if (not first_tick) and bin_idx > (self._last_bin or 0):
            sell_idx = self._highest_held_at_or_below(bin_idx)
            if sell_idx is not None:
                await self._try_sell(
                    grid_idx=sell_idx, current_price=price, direction=bin_direction,
                )

        self._last_bin = bin_idx

    # ─── trade helpers ─────────────────────────────────────────────

    async def _try_buy(self, *, grid_idx: int, current_price: float, direction: str) -> None:
        assert self._ctx is not None
        ctx = self._ctx
        grid_price = self._grids[grid_idx]
        try:
            resp = await ctx.broker.buy(
                strategy=self.name, outcome_id=self._outcome_id,
                shares=self._shares_per_grid,
                max_slippage_bps=int(ctx.config.get("max_slippage_bps", 300)),
            )
        except (RiskRejected, BusinessError, TransientError) as e:
            await ctx.store.log_decision(
                strategy=self.name, outcome_id=self._outcome_id,
                action="skip", reason=f"buy failed: {e}",
                snapshot={"grid": grid_price, "price": current_price},
            )
            return
        self._held[grid_idx] = True
        await ctx.store.log_decision(
            strategy=self.name, outcome_id=self._outcome_id,
            action="buy", reason=f"grid {direction} cross",
            snapshot={
                "grid_idx": grid_idx, "grid_price": grid_price,
                "current_price": current_price,
                "shares": str(resp.shares), "cost": str(resp.cost),
            },
        )

    async def _try_sell(self, *, grid_idx: int, current_price: float, direction: str) -> None:
        assert self._ctx is not None
        ctx = self._ctx
        grid_price = self._grids[grid_idx]
        try:
            resp = await ctx.broker.sell(
                strategy=self.name, outcome_id=self._outcome_id,
                shares=self._shares_per_grid,
                max_slippage_bps=int(ctx.config.get("max_slippage_bps", 300)),
            )
        except (RiskRejected, BusinessError, TransientError) as e:
            await ctx.store.log_decision(
                strategy=self.name, outcome_id=self._outcome_id,
                action="skip", reason=f"sell failed: {e}",
                snapshot={"grid": grid_price, "price": current_price},
            )
            return
        self._held[grid_idx] = False
        await ctx.store.log_decision(
            strategy=self.name, outcome_id=self._outcome_id,
            action="sell", reason=f"grid {direction} cross",
            snapshot={
                "grid_idx": grid_idx, "grid_price": grid_price,
                "current_price": current_price,
                "shares": str(resp.shares), "cost": str(resp.cost),
            },
        )
