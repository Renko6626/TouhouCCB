"""Pure strategy/execution contracts. Monetary values stay Decimal in memory."""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from app.services.fx import amm

FxOrderAction = Literal["buy_gold", "buy_foreign", "sell_foreign"]


@dataclass(frozen=True)
class FxTickFrame:
    tick_id: int
    pair_id: int
    observed_at: datetime
    closes: tuple[Decimal, ...]
    reference_price: Decimal
    signals: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class FxBotSnapshot:
    profile_id: int
    user_id: int
    pair_id: int
    template: str
    params: dict[str, Any]
    cash: Decimal
    foreign_amount: Decimal
    strategy_state: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FxExecutionView:
    gold_reserve: Decimal
    foreign_reserve: Decimal
    buy_fee_rate: Decimal
    sell_fee_rate: Decimal

    def quote_buy(self, gold: Decimal) -> amm.FxQuoteMath:
        return amm.quote_buy(gold, self.gold_reserve, self.foreign_reserve, self.buy_fee_rate)

    def quote_sell(self, foreign: Decimal) -> amm.FxQuoteMath:
        return amm.quote_sell(foreign, self.gold_reserve, self.foreign_reserve, self.sell_fee_rate)

    def quote_buy_exact_out(self, foreign: Decimal) -> amm.FxQuoteMath:
        return amm.quote_buy_exact_out(foreign, self.gold_reserve, self.foreign_reserve, self.buy_fee_rate)


@dataclass(frozen=True)
class FxPreparedSignal:
    reference_price: Decimal
    signal: float = 0.0
    noise: float = 0.0
    values: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FxOrderIntent:
    action: FxOrderAction
    amount: Decimal
    reason: str


@dataclass(frozen=True)
class FxDecision:
    intent: FxOrderIntent | None = None
    state: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass(frozen=True)
class FxFill:
    trade_id: int
    action: FxOrderAction
    input_currency: Literal["gold", "foreign"]
    output_currency: Literal["gold", "foreign"]
    input_amount: Decimal
    output_amount: Decimal
    fee_amount: Decimal
    cash_before: Decimal
    cash_after: Decimal
    foreign_before: Decimal
    foreign_after: Decimal
    executed_at: datetime
    requested_amount: Decimal | None = None


@dataclass(frozen=True)
class FxBotValues:
    cash: Decimal
    wallet: Decimal
    mtm: Decimal
    liquidation_cash: Decimal


@dataclass(frozen=True)
class FxExecutionResult:
    fill: FxFill | None = None
    state: dict[str, Any] = field(default_factory=dict)
    rejection: str | None = None
