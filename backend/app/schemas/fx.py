from datetime import date, datetime
from decimal import Decimal
from typing import Optional, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Storage ceilings mirrored from the ORM columns so a bad request is a 422
#: before it reaches the ledger (``FxTrade.min_out``/``max_gold_in`` are
#: ``Numeric(16,6)``; short principal/requests are ``Numeric(24,6)``).
_MAX_GOLD = Decimal("9999999999.999999")
_MAX_FOREIGN = Decimal("999999999999999999.999999")


def _finite_six(value: Decimal, *, positive: bool = False) -> Decimal:
    if not value.is_finite() or (value <= 0 if positive else value < 0):
        raise ValueError("amount must be finite and positive" if positive
                         else "amount must be finite and non-negative")
    if -value.as_tuple().exponent > 6:
        raise ValueError("amount must have at most 6 fractional digits")
    return value


class FxPairPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    currency_code: str
    currency_name: str
    status: str
    # F9：只减仓是显式管理端选项。默认 false 意味着 paused 仍是"全停"旧语义；
    # 公开响应只多这一个运营状态位，reserve/target/fee 等 operator 字段仍不外露。
    reduce_only: bool = False
    pool_version: int
    created_at: datetime
    updated_at: datetime


class FxPairAdmin(FxPairPublic):
    """Operator view; reserve and target controls stay out of public payloads."""
    archived: bool = False
    gold_reserve: Decimal
    foreign_reserve: Decimal
    initial_price: Decimal
    buy_fee_rate: Decimal
    sell_fee_rate: Decimal
    # Hidden from ``FxPairPublic``: only operators see the pair short capacity.
    short_lending_limit_foreign: Decimal = Decimal("0")


class FxPairAdminDetail(FxPairAdmin):
    """Operator read model: pair controls plus live system treasury state.

    The read-only ``GET /api/v1/admin/fx/pairs`` list uses this so an operator
    page can display draft pairs and treasury budgets without performing a
    write first.  It is admin-only and never appears on the public router.
    """
    gold_balance: Decimal
    foreign_balance: Decimal


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


class FxBorrowBuyQuoteRequest(BaseModel):
    amount: Decimal
    borrow_amount: Decimal

    @field_validator('amount', 'borrow_amount')
    @classmethod
    def valid_amount(cls, value: Decimal) -> Decimal:
        value = _finite_six(value, positive=True)
        if value > _MAX_GOLD:
            raise ValueError('amount exceeds storage limit')
        return value

    @model_validator(mode='after')
    def valid_funding(self):
        if self.borrow_amount > self.amount:
            raise ValueError('borrow_amount cannot exceed amount')
        return self


class FxBorrowBuyRequest(FxBorrowBuyQuoteRequest):
    min_out: Decimal
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator('min_out')
    @classmethod
    def valid_minimum(cls, value: Decimal) -> Decimal:
        value = _finite_six(value)
        if value > _MAX_GOLD:
            raise ValueError('min_out exceeds storage limit')
        return value


class FxBorrowBuyResponse(BaseModel):
    trade_id: int
    pair_id: int
    input_amount: Decimal
    borrow_amount: Decimal
    cash_amount: Decimal
    output_amount: Decimal
    fee_amount: Decimal
    post_price: Decimal
    replay: bool
    created_at: datetime


class FxBorrowBuyQuote(BaseModel):
    pair_id: int
    input_amount: Decimal
    borrow_amount: Decimal
    cash_amount: Decimal
    output_amount: Optional[Decimal] = None
    fee_amount: Optional[Decimal] = None
    effective_price: Optional[Decimal] = None
    post_price: Optional[Decimal] = None
    available_cash: Optional[Decimal] = None
    affordable: Optional[bool] = None
    estimated_debt: Optional[Decimal] = None
    estimated_equity: Optional[Decimal] = None
    estimated_risk_basis: Optional[Decimal] = None
    equity_to_risk_basis: Optional[Decimal] = None
    margin_status: str = 'blocked'
    executable: bool = False
    blocked_reason: Optional[str] = None
    leverage: Optional[Decimal] = None
    daily_rate: Optional[Decimal] = None
    r_initial: Optional[Decimal] = None
    r_maintenance: Optional[Decimal] = None
    expires_at: datetime


class FxShortOpenRequest(BaseModel):
    """One borrow-and-sell short open request (spec §7.1 / §10)."""
    foreign_amount: Decimal
    min_gold_out: Decimal = Decimal("0")
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("foreign_amount")
    @classmethod
    def valid_foreign_amount(cls, value: Decimal) -> Decimal:
        value = _finite_six(value, positive=True)
        if value > _MAX_FOREIGN:
            raise ValueError("foreign_amount exceeds storage range")
        return value

    @field_validator("min_gold_out")
    @classmethod
    def valid_min_gold_out(cls, value: Decimal) -> Decimal:
        value = _finite_six(value)
        if value > _MAX_GOLD:
            raise ValueError("min_gold_out exceeds storage range")
        return value


class FxShortCoverRequest(BaseModel):
    """Exactly one of a fixed quantity or ``cover_all`` (spec §7.2 / §10)."""
    foreign_amount: Optional[Decimal] = None
    cover_all: bool = False
    max_gold_in: Decimal = Decimal("0")
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("foreign_amount")
    @classmethod
    def valid_foreign_amount(cls, value: Optional[Decimal]) -> Optional[Decimal]:
        if value is None:
            return value
        value = _finite_six(value, positive=True)
        if value > _MAX_FOREIGN:
            raise ValueError("foreign_amount exceeds storage range")
        return value

    @field_validator("max_gold_in")
    @classmethod
    def valid_max_gold_in(cls, value: Decimal) -> Decimal:
        value = _finite_six(value)
        if value > _MAX_GOLD:
            raise ValueError("max_gold_in exceeds storage range")
        return value

    @model_validator(mode="after")
    def exactly_one_shape(self) -> "FxShortCoverRequest":
        if (self.foreign_amount is not None) == bool(self.cover_all):
            raise ValueError("exactly one of foreign_amount or cover_all is required")
        return self


class FxShortTradeResponse(BaseModel):
    """Player-facing short action result; money/quantity serialize as strings."""
    trade_id: int
    pair_id: int
    purpose: str
    side: str
    requested_foreign_amount: Optional[Decimal] = None
    cover_all: Optional[bool] = None
    input_amount: Decimal
    output_amount: Decimal
    fee_amount: Decimal
    min_out: Decimal
    max_gold_in: Optional[Decimal] = None
    post_price: Decimal
    replay: bool
    created_at: datetime


class FxShortPositionPublic(BaseModel):
    """Current user's own short row plus a reference full-cover quote.

    Money/quantity fields serialize as decimal strings.  ``reference_cover_cost``
    is ``None`` (never 0/Infinity) when the whole debt cannot be quoted;
    ``risk_status`` distinguishes that from a known-but-not-executable paused
    market.  Operator-only data (treasury stock, pair lending cap) is absent.
    """
    model_config = ConfigDict(from_attributes=True)
    pair_id: int
    currency_code: str
    principal_foreign: Decimal
    interest_foreign: Decimal
    pending_short_debt: Optional[Decimal] = None
    restricted_gold: Decimal
    proceeds_basis_gold: Decimal
    interest_last_accrued_at: Optional[datetime] = None
    reference_cover_cost: Optional[Decimal] = None
    reference_cover_fee: Optional[Decimal] = None
    executable: bool
    risk_status: str
    blocked_reason: Optional[str] = None


class FxShortQuoteRequest(BaseModel):
    """Indicative short quote input (spec §10).

    ``open`` requires a fixed six-place ``foreign_amount``.  ``cover`` requires
    exactly one of a fixed ``foreign_amount`` or ``cover_all=true``.  Invalid
    shapes are rejected by validation before any ledger access.
    """
    action: str
    foreign_amount: Optional[Decimal] = None
    cover_all: bool = False

    @field_validator("action")
    @classmethod
    def valid_action(cls, value: str) -> str:
        normalized = str(value).strip().lower()
        if normalized not in {"open", "cover"}:
            raise ValueError("action must be open or cover")
        return normalized

    @field_validator("foreign_amount")
    @classmethod
    def valid_foreign_amount(cls, value: Optional[Decimal]) -> Optional[Decimal]:
        if value is None:
            return value
        value = _finite_six(value, positive=True)
        if value > _MAX_FOREIGN:
            raise ValueError("foreign_amount exceeds storage range")
        return value

    @model_validator(mode="after")
    def valid_shape(self) -> "FxShortQuoteRequest":
        if self.action == "open":
            if self.foreign_amount is None:
                raise ValueError("foreign_amount is required for open")
            if self.cover_all:
                raise ValueError("cover_all is not valid for open")
        else:
            if (self.foreign_amount is not None) == bool(self.cover_all):
                raise ValueError(
                    "exactly one of foreign_amount or cover_all is required for cover")
        return self


class FxShortQuoteResponse(BaseModel):
    """Advisory short quote; never a promise that the order will execute.

    ``input_amount``/``output_amount`` mirror the AMM direction (open: foreign
    in / gold out; cover: gold in / foreign out).  ``restricted_gold_delta`` is
    signed (positive lock increase on open, negative release on cover).

    Order eligibility and portfolio valuation are separate signals:
    ``executable`` is false only when an order-level gate blocks the action and
    ``blocked_reason`` then names it.  ``risk_blocked_reason`` carries the
    risk/valuation reason (credit freeze, another pair's unknown K, engine
    unavailability) even when ``executable`` is true -- a cover that reduces debt
    stays executable while its portfolio estimate is unavailable, with
    ``estimated_equity``/``estimated_risk_basis`` left ``None``.
    ``expires_at`` is advisory only -- execution always re-quotes under lock.
    """
    model_config = ConfigDict(from_attributes=True)
    pair_id: int
    action: str
    purpose: str
    requested_foreign_amount: Optional[Decimal] = None
    cover_all: Optional[bool] = None
    actual_foreign_amount: Optional[Decimal] = None
    input_amount: Optional[Decimal] = None
    output_amount: Optional[Decimal] = None
    fee_amount: Optional[Decimal] = None
    fee_currency: str
    post_price: Optional[Decimal] = None
    pool_version: Optional[int] = None
    restricted_gold_delta: Optional[Decimal] = None
    available_cash: Optional[Decimal] = None
    affordable: Optional[bool] = None
    estimated_equity: Optional[Decimal] = None
    estimated_risk_basis: Optional[Decimal] = None
    risk_status: str
    margin_status: str
    risk_blocked_reason: Optional[str] = None
    executable: bool
    blocked_reason: Optional[str] = None
    expires_at: datetime


class FxPersonalTrade(FxTradePublic):
    execution_id: Optional[int] = None
    execution_leg: Optional[int] = None
    currency_code: str
    currency_name: str
    is_liquidation: bool
    purpose: str = "spot"
    borrow_amount: Optional[Decimal] = None


class FxSnapshot(BaseModel):
    pair: FxPairPublic
    price: Decimal
    buy_price: Decimal
    sell_price: Decimal
    spread: Decimal
    volume_24h: Decimal = Decimal("0")
    # Additive public cached-history bootstrap for pages without a per-card SSE
    # connection.  ``None``/``False`` means the pair is not backfilled yet and
    # the client must fall back to lightweight ``/chart``; never a fabricated
    # coverage claim.
    history_version: Optional[str] = None
    history_ready: bool = False


class FxNetQuoteRequest(FxQuoteRequest):
    borrow_amount: Decimal = Decimal("0")

    @field_validator("amount", "borrow_amount")
    @classmethod
    def net_bounds(cls, value: Decimal) -> Decimal:
        value = _finite_six(value)
        if value > _MAX_GOLD:
            raise ValueError("amount exceeds storage limit")
        return value

    @model_validator(mode="after")
    def funding(self):
        if self.borrow_amount > self.amount or (self.side != "buy" and self.borrow_amount):
            raise ValueError("financing is only available within a buy budget")
        return self


class FxNetTradeRequest(FxNetQuoteRequest):
    allow_growth: bool = False
    min_out: Decimal = Decimal("0")
    idempotency_key: str = Field(min_length=1, max_length=128)
    expected_economic_version: int = Field(ge=0)

    @field_validator("min_out")
    @classmethod
    def valid_min_out(cls, value: Decimal) -> Decimal:
        value = _finite_six(value)
        if value > _MAX_GOLD:
            raise ValueError("min_out exceeds storage limit")
        return value

    @field_validator("idempotency_key")
    @classmethod
    def valid_key(cls, value: str) -> str:
        if value.startswith("liq:"):
            raise ValueError("idempotency_key prefix is reserved")
        return value


class FxNetLeg(BaseModel):
    purpose: str
    side: str
    input_currency: str
    output_currency: str
    input_amount: Decimal
    output_amount: Decimal
    fee_amount: Decimal
    pre_gold_reserve: Decimal
    pre_foreign_reserve: Decimal
    post_gold_reserve: Decimal
    post_foreign_reserve: Decimal


class FxNetQuote(BaseModel):
    pair_id: int
    side: str
    input_amount: Decimal
    output_amount: Decimal
    fee_amount: Decimal
    post_price: Decimal
    legs: list[FxNetLeg]
    final_direction: str
    wallet_after: Decimal
    foreign_debt_after: Decimal
    cash_delta: Decimal
    gold_debt_delta: Decimal
    restricted_gold_delta: Decimal
    growth_amount: Decimal
    borrow_amount: Decimal
    repaid_gold: Decimal
    executable: bool
    blocked_reason: Optional[str] = None
    economic_version: int
    pool_version: int
    expires_at: datetime


class FxNetTradeResponse(FxNetQuote):
    execution_id: int
    trade_ids: list[int]
    created_at: datetime
    replay: bool = False


class FxLiquidityAmounts(BaseModel):
    gold: Optional[Decimal] = None
    foreign: Optional[Decimal] = None


class FxHoldingsSummary(BaseModel):
    long_users: int
    long_foreign: Decimal
    long_cost_gold: Decimal
    short_users: int
    short_principal_foreign: Decimal
    short_recorded_interest_foreign: Decimal
    short_restricted_gold: Decimal
    interest_is_realtime: bool = False


class FxAdminOverview(BaseModel):
    pair: FxPairAdminDetail
    reference_price: Decimal
    as_of: datetime
    holdings: FxHoldingsSummary


class FxLiquidityPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(pattern="^(fund|withdraw)$")
    gold_amount: Decimal = Decimal("0")
    foreign_amount: Decimal = Decimal("0")

    @field_validator("gold_amount", "foreign_amount")
    @classmethod
    def amount(cls, value: Decimal) -> Decimal:
        value = _finite_six(value)
        # Both pool and treasury currencies use Numeric(16, 6).
        if value > _MAX_GOLD:
            raise ValueError("amount exceeds storage range")
        return value

    @model_validator(mode="after")
    def nonzero(self):
        if not self.gold_amount and not self.foreign_amount:
            raise ValueError("one currency amount must be positive")
        return self


class FxLiquidityPreview(BaseModel):
    pair_id: int
    action: str
    gold_amount: Decimal
    foreign_amount: Decimal
    pool_version: int
    as_of: datetime
    pool_before: FxLiquidityAmounts
    pool_after: FxLiquidityAmounts
    treasury_before: FxLiquidityAmounts
    treasury_after: FxLiquidityAmounts
    price_before: Optional[Decimal]
    price_after: Optional[Decimal]
    price_change_pct: Optional[Decimal]
    executable: bool
    blocked_reason: Optional[str] = None


class FxLiquidityHistoryItem(BaseModel):
    id: int
    ts: datetime
    operator_user_id: Optional[int]
    action: str
    gold_amount: Optional[Decimal]
    foreign_amount: Optional[Decimal]
    pool_before: Optional[FxLiquidityAmounts]
    pool_after: Optional[FxLiquidityAmounts]
    treasury_before: Optional[FxLiquidityAmounts]
    treasury_after: Optional[FxLiquidityAmounts]
    price_before: Optional[Decimal]
    price_after: Optional[Decimal]
    price_change_pct: Optional[Decimal]


class FxLiquidityHistory(BaseModel):
    items: list[FxLiquidityHistoryItem]
    next_before_id: Optional[int] = None


class FxTradeInstruction(BaseModel):
    """Authorized internal instruction, resolved from locked financial state."""
    action: Literal['buy', 'sell', 'close_long', 'close_short']
    amount: Decimal | None = None
    close_percent: Decimal | None = None
    auto_borrow: bool = False
    borrow_limit: Decimal = Decimal('0')

    @model_validator(mode='after')
    def valid_instruction(self):
        _finite_six(self.borrow_limit)
        if self.borrow_limit > _MAX_GOLD:
            raise ValueError('borrow_limit exceeds storage limit')
        if self.action in ('buy', 'sell'):
            if self.amount is None or self.close_percent is not None:
                raise ValueError('buy/sell requires amount only')
            _finite_six(self.amount, positive=True)
            if self.amount > _MAX_GOLD:
                raise ValueError('amount exceeds storage limit')
        else:
            if self.amount is not None or self.close_percent is None:
                raise ValueError('close requires close_percent only')
            if not self.close_percent.is_finite() or not 0 < self.close_percent <= 100:
                raise ValueError('close_percent must be within (0, 100]')
        if self.action != 'buy' and (self.auto_borrow or self.borrow_limit):
            raise ValueError('financing is only available for buy')
        return self
