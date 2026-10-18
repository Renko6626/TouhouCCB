# Task 1 FX core review

Decision: **approve Task 1**, with one nonblocking Minor lifecycle observation. No Critical or Important findings. This approves the bounded FX core migration at `473498e..98e846e`; it does not approve deployment or claim the broader migration is complete.

Reviewed the Task 1 brief/report, approved migration plan, fixed diff and directly relevant unchanged locking/publication helpers. No production or test files changed during review. The concurrent guide document is outside this review.

## Spec compliance and quality

- The new writer has one capacity-256 queue/consumer per pair and a 10-second submit wait. Abandoned queued futures are skipped; a started request may commit after its caller times out. Existing actors reload through their queue; bootstrap loads before queue visibility; unexpected command failures recover by calling `_load_direct` directly.
- Core spot, financed buy, net transaction and short open/cover wrappers materialize typed frozen commands and roll back their producer transaction before submit. No producer session, ORM row or arbitrary callback crosses the queue. The built-in PvE adaptation serializes fill state and applies the existing fill/profile changes inside the writer transaction.
- `writer_ops.execute_command` owns a fresh session per attempt. Dependency discovery releases its transaction before GATE waiting. Financing, cash/debt, holdings, pool/treasury, trades, receipts and audit continue through the existing no-commit kernels in one transaction. Pair/User locks, User `populate_existing` refresh, economic versions and OWNERSHIP remain. No account queue was introduced.
- The successful path flushes and captures immutable descriptors, commits, synchronously installs the committed pair state, then emits account/market-data/publication hints before the next command. Recovery never submits into its own actor. The retained target/collateral GATE is intentional in this intermediate task.
- Startup/shutdown integrate the writer around economic producers. Tests retain the persisted-money, history, slippage and idempotency assertions; the publication test now proves the real nonblocking behavior without assuming task scheduling order.
- The Task 2 op contract is usable: a registered handler owns its fresh transaction, calls no-submission kernels, and installs before returning. Task 2 still must migrate creation/deletion and remaining owners. In particular, production creation is not yet connected to `register_pair`, and the current `_load_direct` cannot represent deletion. Those are acknowledged later-task obligations, not omissions in this Task 1 diff.

## Finding

**Minor — active future is not completed immediately if shutdown cancels recovery.** `backend/app/services/fx/writer.py:205` (recovery through line 208), in combination with `stop` at line 74. If an unexpected op failure has entered `_load_direct` and shutdown cancels the consumer during that await, `CancelledError` originates inside the `except Exception` body. It therefore bypasses the sibling cancellation handler at line 190 and the `future.set_exception` at line 210. The active command has already left the queue, so `stop` only completes the other queued futures. Its caller eventually receives the existing 10-second result-unknown timeout instead of an immediate shutdown response. This does not permit further commands or corrupt funds, and is a low-probability, observable shutdown case; it is not a Task 1 approval blocker. If addressed, keep the change local: cover recovery with the same outer cancellation/finalization behavior that already settles the active future. No additional recovery mechanism is needed.

## Connection warning assessment

The reported warning node, `test_foreign_only_short_blocks_lmsr_writer_buy`, creates an LMSR writer but never submits an FX command. The added FX fixture does not start an actor in that node; its teardown stops the empty singleton. Added production FX session owners use context managers for bootstrap, reload/recovery and each command attempt. Static inspection found no new caller/actor session escaping those contexts.

Ran exactly the reported node in the current worktree and the existing `330f4e6` baseline worktree, using the existing interpreter and pid-local disposable SQLite fixture:

```text
# cwd /tmp/sunyunbo/thccb-single-writer-20261018/backend
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest -q tests/test_fx_short_player_admission.py::test_foreign_only_short_blocks_lmsr_writer_buy
1 passed in 0.22s; no warnings

# cwd /tmp/sunyunbo/thccb-fx-read-checkpoint-20261017/backend
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest -q tests/test_fx_short_player_admission.py::test_foreign_only_short_blocks_lmsr_writer_buy
1 passed in 0.24s; no warnings
```

These runs do not establish the origin of the warning from the multi-file run: GC warnings may surface in a later test. They provide no evidence that this node or the added FX actor/caller sessions leak a connection. The original suite-level warning remains unlocalized and should not be reported as a confirmed baseline warning or a confirmed FX regression.

The worker's 189-test targeted run and subsequent 36-test run were reviewed as reported evidence, not rerun. No PostgreSQL, services, load tests, dependencies, browser automation or broad suites were run. PostgreSQL same-user cross-market contention remains for the controller's scheduled validation.
