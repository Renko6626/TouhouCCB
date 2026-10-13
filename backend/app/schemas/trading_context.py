"""Private trading read contracts. Decimal inputs never cross JSON as floats."""
from typing import Annotated, Literal
from pydantic import BaseModel, Field

class GroupKey(BaseModel):
    product: Literal['fx', 'lmsr']
    id: int = Field(gt=0)

class GroupStamp(BaseModel):
    epoch: str
    revision: str
    input_hash: str

class GroupCoverage(GroupKey):
    stamp: GroupStamp

class FxPricingGroup(GroupCoverage):
    product: Literal['fx'] = 'fx'
    as_of: str
    gold_reserve: str
    foreign_reserve: str
    buy_fee_rate: str
    sell_fee_rate: str
    pool_version: str
    status: str
    archived: bool
    reduce_only: bool

class LmsrPricingGroup(GroupCoverage):
    product: Literal['lmsr'] = 'lmsr'
    as_of: str
    outcome_ids: list[int]
    q_dec: list[str]
    b: str
    status: str
    closes_at: str | None

PricingGroup = Annotated[FxPricingGroup | LmsrPricingGroup, Field(discriminator='product')]

class LmsrPosition(BaseModel):
    market_id: int | None
    outcome_id: int
    amount: str
    cost_basis: str

class FxWalletBasis(BaseModel):
    pair_id: int
    foreign_amount: str
    cost_basis: str

class FxShortBasis(BaseModel):
    pair_id: int
    principal_foreign: str
    interest_foreign: str
    interest_last_accrued_at: str | None
    restricted_gold: str
    proceeds_basis_gold: str

class AccountBasis(BaseModel):
    user_id: int
    economic_version: str
    auto_repay: bool = True
    cash: str
    debt: str
    debt_last_accrued_at: str | None
    credit_frozen: bool
    is_active: bool
    is_bot: bool
    tos_accepted_at: str | None
    last_liquidated_at: str | None
    lmsr_positions: list[LmsrPosition]
    fx_wallets: list[FxWalletBasis]
    fx_shorts: list[FxShortBasis]

class TradingRules(BaseModel):
    rule_version: str
    loan_daily_rate: str
    lmsr_sell_fee_rate: str
    fx_enabled: bool
    fx_short_enabled: bool
    loan_enabled: bool
    unified_credit_enabled: bool
    credit_new_risk_frozen: bool
    risk_engine_ready: bool
    leverage: str | None
    initial_ratio: str | None
    maintenance_ratio: str | None

class TradingContext(BaseModel):
    schema_version: Literal[1] = 1
    account: AccountBasis
    rules: TradingRules
    groups: list[PricingGroup]
    minimum_group_stamps: list[GroupCoverage]
    missing_groups: list[GroupKey]
    as_of: str
    server_time: str

class TradingVersionCheck(BaseModel):
    economic_version: str
    rule_version: str
    group_stamps: list[GroupCoverage]
    missing_groups: list[GroupKey]
    server_time: str

class TradingPrices(BaseModel):
    groups: list[PricingGroup]
    missing_groups: list[GroupKey]
    server_time: str

class AccountChanged(BaseModel):
    event_id: str
    economic_version: str
    reason: str
    occurred_at: str
    operation_ref: str | None = None
