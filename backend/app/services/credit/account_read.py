"""Pure projections of already materialized account valuations."""
from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from app.schemas.fx import FxShortPositionPublic
from app.services.credit.thresholds import RiskThresholds

if TYPE_CHECKING:
    from app.services.credit.valuation import AccountValuation, ShortPositionValuation

ZERO = Decimal('0')


def classify_account_risk(thresholds: RiskThresholds, *, equity: Decimal | None,
                          debt: Decimal, positive_assets: Decimal | None,
                          short_cover: Decimal | None, blocked_reason: str | None
                          ) -> Literal['healthy', 'warning', 'danger', 'blocked']:
    if blocked_reason or equity is None or positive_assets is None or short_cover is None:
        return 'blocked'
    args = dict(equity=equity, debt=debt, positive_assets=positive_assets, short_cover=short_cover)
    return ('danger' if thresholds.triggered_basis(**args) else
            'warning' if not thresholds.admits(**args) else 'healthy')


def _assets(valuation: AccountValuation) -> Decimal:
    return sum((g.value for g in valuation.groups
                if g.role == 'asset_sale' and g.value is not None), ZERO)


def account_risk_fields(valuation: AccountValuation, thresholds: RiskThresholds) -> dict:
    equity, cover = valuation.liquidation_equity, valuation.short_cover_cost
    status = classify_account_risk(thresholds, equity=equity, debt=valuation.debt_effective,
                                  positive_assets=_assets(valuation), short_cover=cover,
                                  blocked_reason=valuation.blocked_reason)
    if valuation.risk_basis is None:
        status = 'blocked'
    return dict(available_cash=valuation.available_cash, restricted_cash=valuation.restricted_cash,
                short_cover_cost=cover, risk_basis=valuation.risk_basis,
                equity_to_risk_basis=(equity / valuation.risk_basis
                    if equity is not None and valuation.risk_basis is not None
                    and valuation.risk_basis > ZERO else None),
                risk_status=status, blocked_reason=valuation.blocked_reason,
                economic_version=valuation.economic_version)


def borrow_blocked_reason(valuation: AccountValuation, thresholds: RiskThresholds, *,
                          loan_enabled: bool, credit_frozen: bool, new_risk_frozen: bool) -> str | None:
    if not loan_enabled:
        return 'loan_disabled'
    if new_risk_frozen:
        return 'frozen_by_operator'
    if credit_frozen:
        return 'credit_frozen'
    equity, cover = valuation.liquidation_equity, valuation.short_cover_cost
    if equity is None or cover is None or valuation.blocked_reason or valuation.risk_basis is None:
        return 'valuation_unavailable'
    args = dict(equity=equity, debt=valuation.debt_effective,
                positive_assets=_assets(valuation), short_cover=cover)
    if not thresholds.admits(**args):
        return 'insufficient_initial_margin'
    if thresholds.max_new_gold_loan(**args) <= ZERO:
        return 'no_borrow_headroom'
    return None


def short_position_fields(position: ShortPositionValuation, *, fx_enabled: bool,
                          unified_enabled: bool) -> dict:
    quote = position.quote
    unknown = quote is None or quote.gold_in is None
    reason = ((position.blocked_reason or 'invalid_short_debt') if quote is None else
              (quote.blocked_reason or 'short_quote_failed') if unknown else
              'fx_disabled' if not fx_enabled else
              'unified_credit_disabled' if not unified_enabled else quote.blocked_reason)
    return dict(pair_id=position.pair_id, currency_code=position.currency_code,
                principal_foreign=position.principal_foreign, interest_foreign=position.interest_foreign,
                pending_short_debt=position.pending_short_debt, restricted_gold=position.restricted_gold,
                proceeds_basis_gold=position.proceeds_basis_gold,
                interest_last_accrued_at=position.interest_last_accrued_at,
                reference_cover_cost=None if unknown else quote.gold_in,
                reference_cover_fee=None if unknown else quote.fee_gold,
                executable=not unknown and reason is None,
                risk_status='blocked' if unknown else 'ok', blocked_reason=reason)


def build_short_positions(valuation: AccountValuation, *, fx_enabled: bool,
                          unified_enabled: bool) -> list[FxShortPositionPublic]:
    return [FxShortPositionPublic(**short_position_fields(
        p, fx_enabled=fx_enabled, unified_enabled=unified_enabled)) for p in valuation.short_positions]
