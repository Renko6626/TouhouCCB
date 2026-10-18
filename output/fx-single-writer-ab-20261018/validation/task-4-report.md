# Task 4b — committed execution risk and unified GATE removal

Approved plan §§2.2–2.4, Task4 brief and preflight followed. Ordinary reads from `809ad3f` were retained. No new account queue, reservation/recovery mechanism, table, outbox, dependency or deployment change. No subagents/services/browser/load runs were started by this implementer. Controller supplied the exclusive disposable PG database and owns its lifecycle.

## Scoped commits and review base

- Immediately before production commit: `6b69f4e` (peer's four credit test files, parent `5978b3b`).
- `6eb1e8d` — `refactor: value execution risk from committed market records`: 17 production paths, including deletion of `credit/gates.py`.
- `b413408` — `test: cover published risk inputs and same-account writer contention`: 28 test/fixture paths, including deletion of obsolete module-only `test_credit_gates.py`.
- Review production alone with `6b69f4e..6eb1e8d`; full Task4b package including peer tests with `5978b3b..b413408`. Peer evidence is `task-4-tests-report.md`. This report is an ignored controller artifact, consistent with the preceding task reports.

## Implementation and self-review

`discover_dependencies` reads fresh own cash, persisted gold debt and accrual time, positive positions/wallets, full live short principal/interest/accrual times, dependency identities and economic_version. Market SQL prices are gone from risk discovery. After the locked handler prepares the final account and before simulation, one synchronous `REGISTRY.capture_committed_records` call supplies the immutable market records. FX asset and cover snapshots use the same record (reserves, buy/sell fees, status, reduce_only, pool version); LMSR uses its record's ordered outcome IDs/q, b, status and close time. The existing maintenance-frozen global loan/sell-fee rules remain separate. Cache versions include the captured stamp and fee; own short debt is stored independently of price availability so a missing price cannot hide an obligation behind the no-debt fast path. Missing required records deny valuation, with no SQL fill, map merge or later recapture.

Execution prepares the full account after User FOR UPDATE in FX spot/financing/net/open/cover, LMSR credit admission, standalone borrowing and admin funds/loans/amnesty. Own User lock is retained through commit. Absolute short debt is constructed from the fresh ledger before simulation. Standalone/financed borrowing computes old interest once and deliberately normalizes the risk accrual timestamp at T before adding new principal. Actor-level pre-lock dependency/retry scaffolding was removed for FX/LMSR admission. Target simulation, real reserves/q, slippage, explicit economic versions, idempotency, ownership and ledger locks remain.

During PG validation a real gap was found in liquidation: `value_user_detailed` still read foreign SQL quote fields after dependency capture. Its existing loader now accepts prepared snapshots; the same existing valuation formulas run against fresh own ledger rows and that fixed quote set. Liquidation pre-check/ranking uses it. Post-action checks refresh the actual target from its own transaction rows and fresh post-ledger, retaining the identical foreign capture. They do not publish uncommitted target data. The optional loader path leaves ordinary/batch valuation unchanged. A focused real-data unit case asserts that a later foreign SQL update/publication cannot enter this post-action check.

All executable generic GATE usage and imports were removed from `backend`. Actor queues provide target serialization; User/Pair/Position/wallet/short/treasury locks, OWNERSHIP and financial histories remain. LMSR bootstrap has a small bootstrap lock matching the FX lifecycle pattern, so concurrent registration cannot create duplicate consumers; existing IDs reload through their consumer. Removed `read_groups_locked`, dead LMSR dependency ContextVar/retry code and the generic module; no compatibility/no-op GATE remains.

Public history/SSE production row-lock and prefix code was preserved. The history PG test's held-commit hook now applies only to the writer task, so its own broadcast reader commit does not accidentally wait for installation. The same test retains FOR SHARE waiting, exact committed volume/quote, old memory during the commit/install gap and history readiness assertions.

## Financial/concurrency evidence

- New real-PG two-market writer financing case gives one user cash40 and requests amount80/borrow70 in each writer. The first User lock is controlled until the second reaches its own real lock. The second risk basis sees cash30, debt70, the first market's exact new holding, economic_version2 (existing separate loan/trade increments). Cash remains sufficient for its cash10 contribution; the second rejects on actual initial margin. Final cash/debt/wallet/version are asserted, not only lock calls.
- New PG stale-prefetch case clears the old LMSR/FX collateral and introduces gold debt70 plus a foreign short10 under the User lock. A check receiving the old prefetch values only the fresh account, denies on margin, reports the real gold/foreign debt and cover cost, and changes no funds/version.
- Existing collateral-GATE-wait loan test now holds a real collateral writer in operation. Borrowing completes against its previous published record and a different pair still trades.
- Exact K, interest, fee direction, actual target reserve, unknown-price, monetary conservation, replay/audit and version assertions are retained. Former ungated-dependency conflict tests now assert actual fresh debt/holdings decisions.
- The approved baseline repayment fixture correction is applied narrowly in the existing PG cover/repay race: repay200 rather than repay_all, preserving all original cash/debt/cover/replay assertions. The PG short-concurrency module passes all15 cases.

### Liquidation timing under unfrozen foreign prices

The existing three-worker100-user scan can return `retry_exhausted` without mutation when other pair writers keep changing the ranked liquidation target between discovery and locked execution. After the SQL/published-input inconsistency was fixed, a focused run produced94 actions and6 legitimate retry-exhausted accounts on its first pass. Production ranking/retry policy was not changed. Following controller direction, the existing test now verifies first-pass successes + retry_exhausted =100 and zero errors/deadlocks, then makes exactly one call to the existing liquidation entry point for each affected user after the concurrent wave finishes. It preserves final100 unique monetary actions, events and users, both FX/LMSR action coverage and financial assertions. This models the existing next sweep tick; it does not promise that every account executes in the first concurrent pass, loop until green, freeze prices, or introduce new production retries.

## Exact final verification commands and results

Interpreter: `/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python` throughout. Unless noted, cwd is worktree `backend`.

### Concentrated related unit slice

```bash
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest -q tests/test_market_read_state.py tests/test_trading_context.py tests/test_trading_stream.py tests/test_market_writer_loop.py tests/test_market_writer_state.py tests/test_writer_buy.py tests/test_writer_sell.py tests/test_writer_admin_ops.py tests/test_writer_e2e.py tests/test_credit_risk.py tests/test_credit_short_risk.py tests/test_credit_execution.py tests/test_credit_liquidate_group.py tests/test_fx_trading_service.py tests/test_fx_trading_in_session.py tests/test_fx_borrow_buy.py tests/test_fx_net_trading.py tests/test_fx_short_trading.py tests/test_fx_short_liquidation.py tests/test_fx_trigger_orders.py tests/test_fx_pve_execution.py tests/test_admin_fx.py tests/test_admin_fx_multi_pair.py tests/test_credit_wp6b_fx_ops.py tests/test_fx_short_player_admission.py --tb=short
```

Result: **349 passed, 5 failed in66.73s**, with one existing SQLAlchemy connection-GC warning. All five failures were outdated test setup/expectations: three expected active-owner SQL fallback while missing/stopped/unavailable, one expected SQL fallback after failed install/recovery, one short-order fixture had not opted into committed price publication. They were corrected without modifying production or dropping financial assertions. The active-read tests now assert503 then explicit bootstrap/reload and the exact q; the short-order module explicitly bootstraps its committed seeds.

Focused rerun from worktree root:

```bash
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest -q backend/tests/test_market_writer_state.py backend/tests/test_writer_admin_ops.py backend/tests/test_fx_trigger_orders.py --tb=short
```

Result: **29 passed, 1 failed in5.61s**; the remaining old install-failure fallback assertion was corrected, then:

```bash
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest -q backend/tests/test_writer_admin_ops.py::test_committed_install_failure_invalidates_before_failed_reload backend/tests/test_credit_wp6b_fx_ops.py --tb=short
```

Result: **4 passed in0.69s**. The 354-case slice was not run again after these test-only corrections; its five failed behaviors all have the focused passing evidence above.

Additional affected files were checked with this completed command from `backend`:

```bash
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest -q tests/test_market_writer_state.py tests/test_writer_admin_ops.py tests/test_fx_trigger_orders.py tests/test_credit_wp6b_admin.py tests/test_credit_wp6b_consumption.py tests/test_fx_short_api.py tests/test_fx_market_data_integration.py --tb=short
```

Result: **86 passed, 5 failed in16.51s**. This process had already imported the same five old tests before their edits, so it reproduced those known failures; its additional34 admin/consumption,24 short-API and3 market-data integration cases all passed. It did not expose an additional production failure.

After the final locked preparation and actor-prefetch cleanup:

```bash
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest -q backend/tests/test_credit_short_risk.py::test_fx_asset_and_cover_reuse_one_published_record backend/tests/test_credit_wp6b_fx_ops.py::test_fund_pair_refreshes_retained_stale_treasury backend/tests/test_credit_wp6a1_admission.py backend/tests/test_credit_wp6a2_lmsr.py --tb=short
```

Result: **29 passed in4.41s**. After adding the liquidation snapshot path, its newly exposed direct-SQL test fixtures were explicitly published and this focused command ran:

```bash
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest -q backend/tests/test_credit_execution.py::test_queued_lmsr_rechecks_all_collateral_and_selection backend/tests/test_fx_short_liquidation.py::test_queued_lmsr_sale_revalidation_persists_unknown_short_block backend/tests/test_fx_short_liquidation.py::test_unknown_run_recovers_at_initial_only_after_pool_funding backend/tests/test_fx_short_liquidation.py::test_blocked_run_resumes_cover_with_current_provenance backend/tests/test_credit_short_risk.py --tb=short
```

Result: **14 passed in2.63s**. The concentrated354-case slice subsequently passed every risk/credit/liquidation case, including the added post-action fixed-foreign-capture test.

### Exclusive real PostgreSQL

Controller-approved URL, used only for the explicitly disposable database:

```text
postgresql+asyncpg://sunyunbo@/fx_single_writer_correctness_test?host=/tmp/sunyunbo/fx-pool-incident-20261017/socket&port=55489
```

```bash
TEST_PG_DATABASE_URL='postgresql+asyncpg://sunyunbo@/fx_single_writer_correctness_test?host=/tmp/sunyunbo/fx-pool-incident-20261017/socket&port=55489' /data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest -q -m pg tests/pg/test_pg_credit_admission_contention.py tests/pg/test_pg_fx_short_concurrency.py tests/pg/test_pg_trading_context.py tests/pg/test_pg_fx_trigger_orders.py tests/pg/test_pg_fx_history_locking.py tests/pg/test_pg_writer_ownership.py tests/pg/test_pg_credit_liquidation.py --tb=short
```

First run: **37 passed, 4 failed, 4 errors in47.33s**. Failures exposed: expected economic_version1 instead of existing financed version2; seed publication prevented the cold-read fixture from being cold; broadcast's own commit was intercepted by the writer-only barrier; liquidation SQL/snapshot mismatch and legitimate post-migration target drift. Trigger fixtures rejected the newly authorized database name. The fixture allowlist was updated only for this controller-approved local database. These issues were corrected as described above.

After corrections and the snapshot-fed liquidation implementation, the same full command resulted in **44 passed, 1 failed in40.49s**. Its remaining failure was the new stale-prefetch fixture's final version assertion accidentally changed by a broad1→2 test replacement; the fixture itself explicitly writes version1 and the financial decision was already correct. It was corrected to assert1, then the exact affected node ran from worktree root:

```bash
TEST_PG_DATABASE_URL='postgresql+asyncpg://sunyunbo@/fx_single_writer_correctness_test?host=/tmp/sunyunbo/fx-pool-incident-20261017/socket&port=55489' /data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest -q -m pg backend/tests/pg/test_pg_credit_admission_contention.py::test_stale_prefetch_cannot_reuse_cleared_collateral_or_omit_new_debt --tb=short
```

Result: **1 passed in0.76s**. Thus all45 retained PG cases have passing evidence, with no skipped cases; the final45-case command was not repeated solely for this assertion-only correction.

The liquidation fix/timing assertion was also verified independently, from worktree root:

```bash
TEST_PG_DATABASE_URL='postgresql+asyncpg://sunyunbo@/fx_single_writer_correctness_test?host=/tmp/sunyunbo/fx-pool-incident-20261017/socket&port=55489' /data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest -q -m pg backend/tests/pg/test_pg_credit_liquidation.py --tb=short
```

Result: **3 passed in10.05s**. The final seven-module run subsequently passed all three again.

### Static/import verification

From worktree root:

```bash
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m compileall -q backend/app backend/tests
git diff --check
```

Both exited0 without output; cached diff checks also passed before both scoped commits.

From `backend`:

```bash
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest --collect-only -q > /tmp/task4-collection.log
```

Exit0: **1528/1587 tests collected, 59 deselected in3.31s**, no import/collection errors. This was collection only, not a full suite run.

A final `ps -eo pid,ppid,args | rg 'pytest|task4'` showed only the inspection command/rg itself. All test processes exited; exclusive PG slot explicitly released to controller. No service/cluster lifecycle was changed by this implementer.

## Remaining references / limits

An executable-import scan finds no `credit.gates` imports or `GATES.*` calls in `backend`. Two whole-repository references are historical artifacts and deliberately untouched:

- `output/fx-dependency-query-ab-20261018/harness/count_server.py` (old counter shim);
- `docs/archive/database-pre-baseline-2026-10-13/backend/scripts/backfill_fx_candles.py` (archived code).

Some historical/current explanatory comments still mention the old GATE; no such executable object remains. The controller's new benchmark shim already handles optional old GATE and new actor observations.

No full repository tests, browser interaction, frontend checks, performance A/B, CI/deployment or production DB actions were performed here. Ordinary-read/quote/frontend evidence remains in Task4a; final performance comparison and independent review remain controller work. The previously deferred Task1 stop-during-recovery future-timing concern was not expanded into a new recovery mechanism.
