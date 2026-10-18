# Task 1 report — FX core writer

Worktree `/tmp/sunyunbo/thccb-single-writer-20261018`, branch `perf/single-writer-markets-20261018`.

Commit `98e846e` (`feat: serialize core FX commands in per-pair writers`). Isolated review boundary: `473498e..98e846e`; the preceding LMSR commit `473498e` is excluded. This report is an ignored local SDD artifact, like the existing task briefs/reports.

## Delivered scope

- Added `fx/writer.py`: one bounded queue/consumer per pair, capacity 256, 10-second submit wait, explicit started-result-unknown response, skipped abandoned commands that have not started, bootstrap-before-queue-visibility, queued external reload/registration of existing actors, direct consumer recovery, unavailable marking, ownership checks, pending-future completion on stop. No account queue or new durable state.
- Added `fx/writer_ops.py`: frozen primitive commands for spot, financed buy, net transaction, short open/cover and built-in PvE fill. Commands hold IDs/Decimal/bool/string values; PvE callback state is serialized JSON. No caller session, ORM or arbitrary callback enters the queue.
- Converted the existing public core wrappers to materialize parameters, release their producer transaction/connection, and submit. Existing `api/v1/fx.py` routes already call these wrappers, so no route or JSON shape change was needed.
- Kept all financial `_in_session` kernels, Pair → User → holdings/treasury locks, `populate_existing` User refreshes, ownership, economic versions, same-transaction debt/interest/ledger/trade/receipt and rollback/idempotency semantics. Transitional target/collateral GATE remains. Credit conflict discovery retries use fresh owned sessions and release the discovery transaction before GATE waiting.
- Writer op sequence is flush → copy immutable final pair snapshot/account descriptors → DB commit → synchronous actor/Registry install → account and market-data/publication hints → response/next command. No await separates commit from install/hints. Fresh spot responses refresh the trade before commit to retain persisted Numeric precision formerly supplied by the wrapper's post-commit refresh.
- Started FX_WRITER before economic producers and stopped it before market-data/publisher shutdown in `main.py`.
- Controller explicitly authorized the small PvE producer adaptation: existing `before_commit` closure became `_apply_fill_in_session`, called only inside the writer transaction. Admission/profile/episode/template revalidation and strategy `on_fill`/last-trade state remain with the financial commit. Observation remains its existing first transaction for Task 2; no arbitrary callback compatibility path remains.

## Fixtures and behavioral coverage

- Root test fixture bootstraps directly seeded pairs before submit and stops the singleton around test DB replacement/teardown. A fixture-only bootstrap lock prevents concurrent test requests from starting the singleton twice; production submit does not create actors.
- Isolated synchronous-SQLite FX fixtures now supply fresh writer sessions against the same database, rather than sharing a producer transaction. One existing valuation scenario refreshes its producer identity-map objects before checking persisted money.
- Existing stale-preflight dependency test injects at the moved writer discovery boundary as well as the lock-in refresh boundary, retaining its complete-GATE, retry and persisted-money assertions.
- Existing publication test blocks the publication hook and proves the response returns before the hook completes. Queueing legitimately allows that worker to start before the producer resumes, so the old task-scheduling assumption was removed without weakening the nonblocking behavior check.
- Existing PvE replay test now exercises an actual BotTrade replay while `on_fill` would raise if repeated; the original trade/amount identity and unchanged persisted financial state checks remain.
- Three new real-kernel writer tests cover command/reload ordering with commit/install-before-hints, committed-install interruption followed by direct recovery and idempotent replay, and a started timeout that commits while an abandoned queued command is skipped. Assertions include actual cash/trade counts/pool versions, not source strings or mock-only financial calculations.
- Minimal controller-approved PG fixture adaptation binds `database.async_session_maker` to the existing disposable `pg_sessionmaker` and the new writer's OWNERSHIP to that fixture's owner. Temporary-DB validation and schema safeguards are unchanged. PostgreSQL has NOT been run for Task 1.

## Verification

All commands used the existing interpreter. Pytest/compileall cwd was the worktree `backend`; SQLite was the process-local disposable database. No dependencies, services, PostgreSQL startup, load tests, browser automation or whole-repository suite.

```text
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest -q tests/test_fx_writer.py tests/test_fx_trading_service.py tests/test_fx_trading_in_session.py tests/test_fx_borrow_buy.py tests/test_fx_net_trading.py tests/test_fx_short_trading.py tests/test_fx_short_api.py tests/test_fx_short_player_admission.py tests/test_fx_pve_execution.py tests/test_fx_publication_e2e.py tests/test_fx_valuation.py tests/test_fx_audit_replay.py tests/test_fx_market_data_integration.py
189 passed, 1 warning in 33.19s

# After the fixture-only bootstrap lock was added:
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest -q tests/test_fx_writer.py tests/test_fx_borrow_buy.py tests/test_fx_pve_execution.py tests/test_fx_short_player_admission.py
36 passed, 1 warning in 6.80s

/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m compileall -q app/services/fx/writer.py app/services/fx/writer_ops.py app/services/fx/trading.py app/services/fx/borrow_buy.py app/services/fx/net_trading.py app/services/fx/shorts.py app/services/pve_fx/executor.py app/main.py tests/conftest.py tests/pg/conftest.py
exit 0

git diff --check
git diff --cached --check
exit 0
```

The warning node in both final runs was `tests/test_fx_short_player_admission.py::test_foreign_only_short_blocks_lmsr_writer_buy`:

```text
SAWarning: The garbage collector is trying to clean up non-checked-in connection <AdaptedConnection <aiosqlite.core.Connection object at [run-specific address]>>, which will be dropped, as it cannot be safely terminated. Please ensure that SQLAlchemy pooled connections are returned to the pool explicitly, either by calling ``close()`` or by using appropriate context managers to manage their lifecycle.
```

Baseline warning status is **not established**: I did not run that node on a baseline checkout. Assertions passed; warning origin/resource impact remains for the reviewer/controller to assess without broadening this task.

Earlier diagnostic runs exposed and corrected the missing seeded-pair bootstrap, an invalidation key-type typo, fresh spot Decimal precision, publication scheduling assumption, stale fixture identity maps, and the moved dependency-injection boundary. No financial, history, version, slippage or idempotency assertions were removed or relaxed.

## Self-review and handoff

Read the full approved plan and prior commit-owner audit, then re-read all scoped production/fixture diffs before staging. Checked that enqueue holds no producer transaction, financial handlers call only non-submitting kernels, replay materializes before rollback, all core mutations stage the pair, commit/install failures invalidate and recover before the next command, failed install sends no account hint, and User locking remains in each financial kernel. Staged only the 17 Task 1 files; no LMSR/account_events/registry/risk file changes were included. No new architecture decision is outstanding.

Task 2 integration contract: `FX_WRITER.register_op(CommandType, async_op)` installs a callable `async_op(writer, command)` returning its public response. The op must own a fresh session and retain the transitional GATE until its commit + `writer.install_committed(FxReadState)` + hints complete. Use the direct `_in_session` kernels, not public submitting wrappers. The old `commit_fx_changes` helper swallows installation errors, so Task 2 writer ops must not use that behavior to continue with an unhealthy actor. New pair creation must commit/close before `register_pair`; existing-pair `register_pair` and public `reload_state` queue behind prior commands. Consumer recovery calls `_load_direct`, never submit.

Remaining approved work belongs to subsequent tasks: condition orders, full FX liquidation rounds, liquidity/admin/writeoff/observation owners, creation/deletion integration, ordinary/cold read and cross-market risk semantics, then GATE removal. In particular, production creation is not yet wired to bootstrap a new actor in this intermediate commit. This Task 1 commit is not a deployable completion of the broader migration. PostgreSQL concurrency and same-user cross-market account locking evidence remain pending the controller's scheduled validation.
