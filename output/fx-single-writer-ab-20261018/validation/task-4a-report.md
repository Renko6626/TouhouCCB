# Task 4a ordinary immutable reads

Approved plan §§2.2/2.4 and task-4-preflight.md followed. Independent ordinary-read slice only; FX writes, finance/risk, general GATE cleanup, main/conftest and public history production paths were not changed. Shared worktree peers' edits were preserved and excluded from staging.

## Contract for Task 4b

`PricingRegistry.capture_committed_records(keys: Sequence[GroupKey]) -> dict[tuple[str, int], PricingRecord]` is synchronous, with no await, SQL, GATE acquisition, writer queue request, installation or invalidation. It returns existing frozen PricingRecord references for requested identities only when that product is memory-enabled, OWNERSHIP is the writable owner, the process is not read-only, and the actor lifecycle is not marked unavailable. It inspects only actor `unavailable`, never mutable q/reserves. Missing keys are omitted. Risk must reject missing dependencies; it must not supplement the capture with SQL or a later registry read. Captured references survive later publications unchanged.

## Changes

- Ordinary read_groups/refresh_groups/read_fx/read_lmsr no longer acquire market GATES. The existing internal read_groups_locked caller precondition remains for Task 4b to remove with the remaining execution freeze flow.
- Warm reads return complete published records, including the previous record during commit-to-install gaps. Cold SQL is response-only and never installs or invalidates the writer projection. A concurrently installed healthy record wins. Existing unavailable/deleted actor lifecycle prevents late SQL from reviving it. Active owner records missing healthy publication return HTTP 503; terminal/nonowner/read-only response fallback remains. Existing hash/epoch/revision behavior is retained.
- LMSR quote uses one immutable registry state for q/b/status/closes_at; SQL is limited to outcome identity/labels and the existing fee rule. The request's metadata session is closed before a separate cold fallback session can be opened.
- Account before/basis/after economic_version stability, dependency authorization and JSON structure are preserved. Backend/schema/store comments now describe pricing floors independently from account economic_version.
- Existing read tests now exercise prompt old-record responses, immutable captured records, response-only terminal fallback, late SQL versus publication/unavailability/deletion, quote isolation from mutable writer q, and private first-frame/context fresh account with the previous published pricing record.
- Authorized PG read tests were adapted, not executed: explicit writer/bootstrap readiness replaces ordinary-read prewarming; writer-owned commit hooks replace producer-session commit hooks; context commit-gap and cold-race assertions follow independent publication semantics. Public history FOR SHARE/prefix/volume/price assertions remain intact.

## Validation commands and actual results

Commands below ran from the indicated directories using the existing interpreter/dependencies; no installs, PostgreSQL service, load run, browser, or full suite was started.

From `backend`:

```text
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest -q tests/test_market_read_state.py tests/test_trading_context.py tests/test_trading_stream.py
```

Initial result: 2 failed, 25 passed (test adaptation errors: explicit bootstrap no longer counted as registry SQL, QuoteResponse after_prices are model objects). After correcting those assertions and extending the existing first-frame test, rerun result: **27 passed in 3.73s**.

After closing quote metadata session before registry fallback:

```text
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest -q tests/test_market_read_state.py tests/test_market_anti_bot_integration.py tests/test_core_audit_3.py::test_quote_matches_fill_and_respects_closes_at tests/test_configurable_economy.py::test_quote_sell_applies_configured_fee tests/test_market_gating.py::test_quote_not_gated
```

Result: **22 passed in 4.09s**. These include existing quote/fill equality, fee, closes_at, authorization and anti-bot checks.

```text
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m py_compile app/services/trading_prices.py app/services/credit/trading_context.py app/schemas/trading_context.py app/api/v1/market.py tests/pg/test_pg_trading_context.py tests/pg/test_pg_fx_history_locking.py
```

Result: exit 0, no output.

From `thccb-frontend`:

```text
ln -s /data/sunyunbo/www/TouhouCCB/thccb-frontend/node_modules node_modules
npm run test:unit -- src/stores/__tests__/tradingSession.spec.ts
```

Result: **19 passed**, one test file, 847ms. node_modules is an ignored symlink to existing root dependencies.

From worktree root:

```text
git diff --check -- backend/app/api/v1/market.py backend/app/schemas/trading_context.py backend/app/services/credit/trading_context.py backend/app/services/trading_prices.py backend/tests/test_market_read_state.py backend/tests/test_trading_context.py backend/tests/test_trading_stream.py backend/tests/pg/test_pg_trading_context.py backend/tests/pg/test_pg_fx_history_locking.py thccb-frontend/src/stores/tradingSession.ts
```

Result: exit 0, no output. Self-review covered owned production/test diffs, writer lifecycle checks and preserved history assertions.

## Pending exclusive PostgreSQL evidence

No PostgreSQL test result is claimed. Controller should run the two existing files with `-m pg` after peer FX writer/fixture integration, especially:

- `tests/pg/test_pg_trading_context.py::test_context_returns_fresh_account_before_snapshot_install`
- `tests/pg/test_pg_trading_context.py::test_old_cold_read_cannot_overwrite_real_trade`
- `tests/pg/test_pg_trading_context.py::test_admin_fee_snapshot_uses_postgres_stored_precision`
- `tests/pg/test_pg_fx_history_locking.py::test_public_frame_waits_for_row_and_reads_commit_before_memory_install[initial]`
- `tests/pg/test_pg_fx_history_locking.py::test_public_frame_waits_for_row_and_reads_commit_before_memory_install[broadcast]`

Peer Task 2 owns pg/conftest writer teardown/readiness. The read tests explicitly bootstrap before active-owner reads; they do not grant ordinary readers publication authority.

## Scoped commit

HEAD immediately before first scoped commit: `4ce408118d95efd5e2480aff4bc3105379f5455b`. Resulting owned commit: `809ad3f` (`perf: read immutable published market snapshots without gates [skip ci]`). Only the ten owned source/test paths listed in the diff-check command were staged/committed; no peer FX commit was included. The ignored `.superpowers` report is left as controller evidence. `git diff --cached --check` also exited 0 immediately before commit.
