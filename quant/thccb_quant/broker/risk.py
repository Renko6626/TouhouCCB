"""RiskGuard: 下单前最后一道闸。spec §5.1-§5.2。"""
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from thccb_quant.errors import RiskRejected


@dataclass
class RiskConfig:
    single_order_cap_cny: Decimal
    daily_loss_cap_cny: Decimal
    daily_turnover_cap_cny: Decimal
    max_slippage_bps: int
    min_seconds_between_orders: float  # 支持亚秒粒度（如 0.5）应对散户挤兑


class RiskGuard:
    def __init__(self, config: RiskConfig, state_dir: Path):
        self._cfg = config
        self._state_dir = state_dir
        self._last_order_ts: dict[int, float] = {}

    def _kill_switch_active(self) -> bool:
        return (self._state_dir / "KILL").exists()

    def check(
        self,
        *,
        outcome_id: int,
        side: str,
        cost: Decimal,
        max_slippage_bps: int,
        turnover_today: Decimal,
        net_pnl_today: Decimal,
    ) -> None:
        if self._kill_switch_active():
            raise RiskRejected("kill switch active")

        if max_slippage_bps > self._cfg.max_slippage_bps:
            raise RiskRejected(
                f"slippage {max_slippage_bps} bps > config {self._cfg.max_slippage_bps}"
            )

        if cost.copy_abs() > self._cfg.single_order_cap_cny:
            raise RiskRejected(
                f"single order cost {cost} > cap {self._cfg.single_order_cap_cny}"
            )

        if net_pnl_today <= -self._cfg.daily_loss_cap_cny:
            raise RiskRejected(
                f"daily loss {net_pnl_today} reached cap {-self._cfg.daily_loss_cap_cny}"
            )

        if turnover_today + cost.copy_abs() > self._cfg.daily_turnover_cap_cny:
            raise RiskRejected(
                f"daily turnover would exceed cap {self._cfg.daily_turnover_cap_cny}"
            )

        last = self._last_order_ts.get(outcome_id)
        if last is not None and time.monotonic() - last < self._cfg.min_seconds_between_orders:
            raise RiskRejected(
                f"cooldown: last order on outcome {outcome_id} too recent"
            )

    def mark_order(self, *, outcome_id: int) -> None:
        self._last_order_ts[outcome_id] = time.monotonic()
