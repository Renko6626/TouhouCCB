"""In-memory accounts around the production constant-product math."""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from backend.app.services.fx.amm import FxQuoteMath, quote_buy, quote_buy_exact_out, quote_sell

ZERO = Decimal("0")


def nonnegative(value: Decimal) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < ZERO:
        raise ValueError("balances must be finite, nonnegative Decimal values")


@dataclass
class Account:
    name: str
    gold: Decimal = ZERO
    foreign: Decimal = ZERO

    def __post_init__(self) -> None:
        nonnegative(self.gold)
        nonnegative(self.foreign)


@dataclass(frozen=True)
class Order:
    """Buy uses gold input; sell uses foreign input; buy_exact_out uses foreign output."""

    side: Literal["buy", "sell", "buy_exact_out"]
    amount: Decimal


@dataclass
class Pool:
    gold: Decimal
    foreign: Decimal
    fee_rate: Decimal = Decimal("0.0005")
    fees_gold: Decimal = field(default=ZERO, init=False)
    fees_foreign: Decimal = field(default=ZERO, init=False)

    def __post_init__(self) -> None:
        nonnegative(self.gold)
        nonnegative(self.foreign)
        nonnegative(self.fee_rate)
        if self.gold == ZERO or self.foreign == ZERO or self.fee_rate >= 1:
            raise ValueError("reserves must be positive and fee_rate must be below 1")

    @property
    def price(self) -> Decimal:
        return self.gold / self.foreign

    def trade(self, account: Account, order: Order) -> FxQuoteMath:
        if order.side in ("buy", "buy_exact_out"):
            buy = quote_buy_exact_out if order.side == "buy_exact_out" else quote_buy
            quote = buy(order.amount, self.gold, self.foreign, self.fee_rate)
            if account.gold < quote.input_amount:
                raise ValueError("insufficient gold")
            account.gold -= quote.input_amount
            account.foreign += quote.output_amount
            self.fees_gold += quote.fee_amount
        elif order.side == "sell":
            quote = quote_sell(order.amount, self.gold, self.foreign, self.fee_rate)
            if account.foreign < quote.input_amount:
                raise ValueError("insufficient foreign")
            account.foreign -= quote.input_amount
            account.gold += quote.output_amount
            self.fees_foreign += quote.fee_amount
        else:
            raise ValueError("side must be buy or sell")
        self.gold = quote.post_gold_reserve
        self.foreign = quote.post_foreign_reserve
        return quote

    def inject(self, *, gold: Decimal = ZERO, foreign: Decimal = ZERO) -> None:
        """Issue currency directly into the pool; not a swap or a trade."""
        nonnegative(gold)
        nonnegative(foreign)
        if gold == ZERO and foreign == ZERO:
            raise ValueError("injection must add currency")
        self.gold += gold
        self.foreign += foreign
