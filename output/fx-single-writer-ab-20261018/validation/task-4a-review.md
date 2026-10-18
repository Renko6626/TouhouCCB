# Task 4a review — ordinary immutable snapshot reads

Reviewed fixed base `4ce408118d95efd5e2480aff4bc3105379f5455b` through head `809ad3f`, using `review-4ce4081..809ad3f.diff`, `task-4a-brief.md`, `task-4a-report.md`, the approved plan §§2.2/2.4 and the Task 4 preflight. Uncommitted peer FX changes were excluded. This is a spec-compliance and code-quality review of Task 4a, not approval of the remaining risk migration or GATE removal.

## Findings

- Critical: none.
- Important: none.
- Minor: none.

## Spec compliance and quality evidence

- `backend/app/services/trading_prices.py:183–191`: ordinary reads capture published records directly and otherwise use the response-only loader. Neither branch enters a GATE or submits writer work. The retained `read_groups_locked` precondition at lines 209–214 is explicitly reserved for Task 4b and is not on the ordinary read path.
- `backend/app/services/trading_prices.py:147–174`: cold reads never install or invalidate `_entries`. They check for a healthy publication both before and after SQL, prefer a publication that arrives during the query, and reject an unavailable actor rather than returning stale SQL. A missing healthy active-owner publication returns 503; terminal, nonowner and read-only database fallbacks create only a response record. Existing stamp revision/hash transitions remain in the registry metadata.
- `backend/app/services/trading_prices.py:103–124`: `capture_committed_records(keys: Sequence[GroupKey]) -> dict[tuple[str, int], PricingRecord]` is synchronous and performs no SQL, GATE acquisition, queue submission, installation or invalidation. It applies memory-product/ownership/write/read-only conditions and inspects only writer lifecycle availability. Missing and unavailable dependencies are omitted, and its docstring directs execution risk to reject them rather than perform a later read. Returned frozen records retain immutable state and stamps across later publications (`backend/app/services/market_read_state.py:10–60`).
- `backend/app/api/v1/market.py:661–680`: quote arithmetic receives ordered q, b, status and closes_at from one registry state. SQL supplies identities and labels, and the metadata session closes before a separate registry fallback session can open. The existing sell-fee rule and response construction remain in place; quote no longer reads mutable writer q.
- `backend/app/services/credit/trading_context.py:80–110`: the context before/basis/after economic-version stability check and pricing authorization/dependency behavior remain intact. Schema and frontend edits are comments only. `minimum_group_stamps` now explicitly describes the included price floor independently of account economic_version, without changing JSON shape.
- The unit-test changes cover complete old publication reads while a writer is held, captured-record immutability, terminal response-only fallback, late SQL versus publication/unavailability/deletion, quote isolation from in-flight writer q, and a fresh private first frame/account alongside the previous price publication. They exercise observable responses rather than source-string assertions.
- The PG changes adapt bootstrap readiness and commit interception to writer-owned transactions. `backend/tests/pg/test_pg_fx_history_locking.py:207–229` retains the FOR SHARE wait and committed volume/price/history assertions. Historical production paths were not modified.

## Verification limits

This review performed fixed-revision source/diff inspection only. No test suite, PostgreSQL service, browser flow, dependency installation or load run was started. The implementation report records the existing focused checks as 27 passed for market-read/context/private-stream tests, 22 passed for market-read/quote-related tests, 19 frontend store tests passed, and successful compilation/diff checks. Those are author-provided results, not independently rerun results.

The two adapted PG files still require the controller's exclusive PG run after peer writer/fixture integration, as the implementation report states. This review does not claim a PG pass. Actual execution-time risk consumption and deletion of the remaining GATE machinery belong to Task 4b and were not expanded into this slice.

## Verdict

**Approved for Task 4a.** No concrete correctness or maintainability issue requires a revision in the reviewed scope. Proceed with the already planned peer integration and pending PG validation; this verdict does not mark the full migration complete.
