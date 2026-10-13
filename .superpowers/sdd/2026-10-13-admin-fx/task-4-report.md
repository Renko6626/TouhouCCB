# Task 4 implementation report

Implemented within approved admin API/read-service boundary. No changes to liquidity writes, pool event writes, publication, migrations, public responses, fund/withdraw identity or locking contracts. No production environment, database, deployment, browser, merge, push or subagent actions.

## Exact frontend interface

All routes prefix `/api/v1/admin/fx`, superuser only. Decimal fields serialize as decimal strings; optional Decimal fields serialize as null when unknown. Prices/pct use Decimal precision 48; pct is percentage units (10 means +10%). `as_of` is UTC statement time (PostgreSQL statement_timestamp; SQLite current_timestamp).

GET `/pairs/{pair_id}/overview` → `FxAdminOverview`:
- `pair: FxPairAdminDetail`: `id:int, currency_code:str, currency_name:str, status:str, reduce_only:bool, pool_version:int, created_at:datetime, updated_at:datetime, archived:bool, gold_reserve:Decimal, foreign_reserve:Decimal, initial_price:Decimal, buy_fee_rate:Decimal, sell_fee_rate:Decimal, short_lending_limit_foreign:Decimal, gold_balance:Decimal, foreign_balance:Decimal`.
- `reference_price:Decimal, as_of:datetime, holdings:FxHoldingsSummary`.
- `FxHoldingsSummary`: `long_users:int, long_foreign:Decimal, long_cost_gold:Decimal, short_users:int, short_principal_foreign:Decimal, short_recorded_interest_foreign:Decimal, short_restricted_gold:Decimal, interest_is_realtime:bool=false`.
- All wallets/short rows included regardless of user status. User counts are positive foreign holdings / positive principal+recorded interest. Interest is recorded, without realtime accrual or risk valuation.

POST `/pairs/{pair_id}/liquidity-preview` body `FxLiquidityPreviewRequest`:
- `action:str` required pattern fund|withdraw; `gold_amount:Decimal=0, foreign_amount:Decimal=0`; extra fields forbidden.
- Finite nonnegative amounts, <=6 fractional places, at least one positive, both <=9999999999.999999. Invalid body is 422.
- Example structural body: `{action:"fund", gold_amount:"10", foreign_amount:"0"}`.
- Returns `FxLiquidityPreview`: `pair_id:int, action:str, gold_amount:Decimal, foreign_amount:Decimal, pool_version:int, as_of:datetime, pool_before:FxLiquidityAmounts, pool_after:FxLiquidityAmounts, treasury_before:FxLiquidityAmounts, treasury_after:FxLiquidityAmounts, price_before:Optional[Decimal], price_after:Optional[Decimal], price_change_pct:Optional[Decimal], executable:bool, blocked_reason:Optional[str]=None`.
- `FxLiquidityAmounts`: `gold:Optional[Decimal]=None, foreign:Optional[Decimal]=None`. Current preview balances are known values, including negative hypothetical blocked results; price is null when a hypothetical pool is nonpositive.
- Reads only pair/treasury; initial and current pool/treasury separately receive the input amounts. Archived, nonpositive resulting pool, insufficient treasury and resulting storage overflow return executable=false/readable reason. No writes/audit, no holdings or valuation work, no guarantee of later execution price.

GET `/pairs/{pair_id}/liquidity-history?before_id=…&limit=20` → `FxLiquidityHistory`:
- Query `before_id:Optional[int]=None` >=1; `limit:int=20` 1..100. Invalid query 422.
- `items:list[FxLiquidityHistoryItem], next_before_id:Optional[int]=None` (null at final page).
- Item: `id:int, ts:datetime, operator_user_id:Optional[int], action:str` (fund|withdraw|initial_issuance), `gold_amount:Optional[Decimal], foreign_amount:Optional[Decimal], pool_before:Optional[FxLiquidityAmounts], pool_after:Optional[FxLiquidityAmounts], treasury_before:Optional[FxLiquidityAmounts], treasury_after:Optional[FxLiquidityAmounts], price_before:Optional[Decimal], price_after:Optional[Decimal], price_change_pct:Optional[Decimal]`.
- Descending audit ID, strict `< before_id`, limit+1 continuation detection; pair/ref/type predicates use existing compound audit index. Reads retained audits even if pair was deleted. Initial issuance has null before/price_before/pct. Missing, invalid or nonfinite stored values stay null; no fabricated zeroes.

## Exact service helper signatures

In `backend/app/services/fx/admin_reads.py`:
- `async overview(db: AsyncSession, pair_id: int) -> FxAdminOverview`
- `async liquidity_preview(db: AsyncSession, pair_id: int, req: FxLiquidityPreviewRequest) -> FxLiquidityPreview`
- `async liquidity_history(db: AsyncSession, pair_id: int, before_id: int | None, limit: int) -> FxLiquidityHistory`
- `_state_query(db: AsyncSession, pair_id: int)` returns SQL select with fresh column projection, pair/treasury and statement timestamp.
- `_prices(before, after)` returns `(Optional[Decimal], Optional[Decimal], Optional[Decimal])` prices/pct.
- `_audit_decimal(value)` returns finite Decimal or None.
- `_audit_amounts(payload, name)` returns FxLiquidityAmounts or None.

Overview executes one SQL statement containing two separately aggregated subqueries. It has no wallet×short fanout or multiple READ COMMITTED queries. Column projection avoids identity-map staleness.

## Validation and review

Final command from backend: `venv/bin/python -m pytest tests/test_admin_fx.py tests/test_admin_fx_multi_pair.py tests/test_credit_wp6b_fx_ops.py -q` — 35 passed. Existing admin fixture exercised: 2 users with both long/short rows (no duplication), gold-only and foreign-only price direction/numeric pct, dual issuance balances, insufficient pool/treasury, precision/input/range validation, resulting storage overflow, no preview mutations/audits/version changes, archived block, all new endpoint permissions, currency-filtered history and keyset continuation, initial issuance and malformed legacy audit unknown fields.

Self-review: reviewed SQL shape, independent aggregate boundaries, numeric ceilings (both pool/treasury currencies Numeric(16,6)), existing write contracts unchanged, strict cursor and limit+1 handling. `git diff --check` passed.

No PostgreSQL live integration or browser testing performed. SQLite behavioral tests cover production read helpers via real router/session. Preview is advisory; existing write amount validation/service behavior preserved as requested, so preview storage blocking does not introduce a new write contract. Global trading flag remains existing `/config`, independently fetched by frontend. No architecture decisions pending.
