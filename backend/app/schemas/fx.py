from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FxPairPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    currency_code: str
    currency_name: str
    status: str
    pool_version: int
    created_at: datetime
    updated_at: datetime


class FxPairAdmin(FxPairPublic):
    """Operator view; reserve and target controls stay out of public payloads."""
    gold_reserve: Decimal
    foreign_reserve: Decimal
    target_price: Decimal
    initial_price: Decimal
    target_min: Decimal
    target_max: Decimal
    buy_fee_rate: Decimal
    sell_fee_rate: Decimal


class FxPairAdminDetail(FxPairAdmin):
    """Operator read model: pair controls plus live system treasury state.

    The read-only ``GET /api/v1/admin/fx/pairs`` list uses this so an operator
    page can display draft pairs and treasury budgets without performing a
    write first.  It is admin-only and never appears on the public router.
    """
    gold_balance: Decimal
    foreign_balance: Decimal
    daily_spend: Decimal
    spend_date: Optional[date] = None


class FxWalletPublic(BaseModel):
    """Current user's position in one pair; public schema, no hidden fields."""
    model_config = ConfigDict(from_attributes=True)
    pair_id: int
    foreign_amount: Decimal
    cost_basis: Decimal
    updated_at: Optional[datetime] = None


class FxQuote(BaseModel):
    pair_id: int
    side: str
    input_amount: Decimal
    output_amount: Decimal
    fee_amount: Decimal
    effective_price: Decimal
    post_price: Decimal
    expires_at: Optional[datetime] = None


class FxQuoteRequest(BaseModel):
    side: str
    amount: Decimal

    @field_validator("side")
    @classmethod
    def valid_side(cls, value: str) -> str:
        if value not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        return value

    @field_validator("amount")
    @classmethod
    def positive_amount(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("amount must be finite and positive")
        if -value.as_tuple().exponent > 6:
            raise ValueError("amount must have at most 6 fractional digits")
        return value


class FxTradeRequest(FxQuoteRequest):
    min_out: Decimal
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("min_out")
    @classmethod
    def valid_min_out(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 0:
            raise ValueError("min_out must be finite and non-negative")
        if -value.as_tuple().exponent > 6:
            raise ValueError("min_out must have at most 6 fractional digits")
        return value


class FxTradePublic(BaseModel):
    """Public trade view.

    The internal execution ``source`` is deliberately omitted.  Player orders
    (``player``) and system orders (``system_event`` / ``system_noise`` /
    ``system_target``) share the public feed, so exposing ``source`` would leak
    the intervention origin.  Operators still see it through the admin-only
    ``Intervention`` schema.
    """
    model_config = ConfigDict(from_attributes=True)
    id: int
    pair_id: int
    side: str
    input_amount: Decimal
    output_amount: Decimal
    fee_amount: Decimal
    post_price: Decimal
    created_at: datetime


class FxSnapshot(BaseModel):
    pair: FxPairPublic
    price: Decimal
    buy_price: Decimal
    sell_price: Decimal
    spread: Decimal
    volume_24h: Decimal = Decimal("0")


class FxEventPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    pair_id: int
    status: str
    title: str
    body: str
    kind: str
    published_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class FxEventAdmin(FxEventPublic):
    shock_ratio: Optional[Decimal] = None
    first_reaction_ratio: Optional[Decimal] = None
    window_sec: Optional[int] = None
    budget: Optional[Decimal] = None
    scheduled_at: Optional[datetime] = None
    parameter_snapshot: Optional[dict] = None
    error_message: Optional[str] = None
    operator_user_id: Optional[int] = None
