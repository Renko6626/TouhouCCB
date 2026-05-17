"""LiveBroker: 真实下单 + 风控 + 幂等。spec §5.3。"""
from datetime import datetime, timezone
from decimal import Decimal

from thccb_quant.broker.base import Broker
from thccb_quant.broker.risk import RiskGuard
from thccb_quant.client.rest import RestClient, OrderResponse
from thccb_quant.errors import BusinessError, RiskRejected
from thccb_quant.state.store import Store


class LiveBroker(Broker):
    def __init__(self, *, rest: RestClient, risk: RiskGuard, store: Store):
        self._rest = rest
        self._risk = risk
        self._store = store

    async def _common(
        self, *, strategy: str, outcome_id: int, shares: Decimal,
        side: str, max_slippage_bps: int,
    ) -> OrderResponse:
        quote = await self._rest.quote(outcome_id=outcome_id, shares=shares, side=side)
        cost = quote.net

        today = datetime.now(timezone.utc).date().isoformat()
        stats = await self._store.get_daily_stats(today)
        self._risk.check(
            outcome_id=outcome_id, side=side, cost=cost,
            max_slippage_bps=max_slippage_bps,
            turnover_today=Decimal(stats["gross_turnover"]),
            net_pnl_today=Decimal(stats["net_pnl"]),
        )

        if await self._store.has_recent_duplicate(
            strategy, outcome_id, side, shares,
            within_sec=5, statuses=("success", "dryrun"),
        ):
            raise RiskRejected("duplicate within 5s")

        try:
            if side == "buy":
                resp = await self._rest.buy(
                    outcome_id=outcome_id, shares=shares,
                    max_slippage_bps=max_slippage_bps,
                )
            else:
                resp = await self._rest.sell(
                    outcome_id=outcome_id, shares=shares,
                    max_slippage_bps=max_slippage_bps,
                )
        except BusinessError as e:
            await self._store.log_order(
                strategy=strategy, outcome_id=outcome_id, side=side,
                shares=shares, status="failed", error=str(e),
            )
            raise

        await self._store.log_order(
            strategy=strategy, outcome_id=outcome_id, side=side,
            shares=shares, price=quote.avg_price, cost=resp.cost,
            status="success",
        )
        await self._store.add_turnover(resp.cost.copy_abs())
        self._risk.mark_order(outcome_id=outcome_id)
        return resp

    async def buy(self, *, strategy, outcome_id, shares, max_slippage_bps):
        return await self._common(
            strategy=strategy, outcome_id=outcome_id, shares=shares,
            side="buy", max_slippage_bps=max_slippage_bps,
        )

    async def sell(self, *, strategy, outcome_id, shares, max_slippage_bps):
        return await self._common(
            strategy=strategy, outcome_id=outcome_id, shares=shares,
            side="sell", max_slippage_bps=max_slippage_bps,
        )
