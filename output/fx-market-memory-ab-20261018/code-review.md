# Final whole-branch review: committed market reads

Reviewed **a820157..330f4e6** in `/tmp/sunyunbo/thccb-fx-read-checkpoint-20261017` against the approved design and implementation plan. Inputs included the supplied whole-branch diff, current source and immediate callers, `progress.md`, Task 1–4 reports/reviews, and the Task 4 fix/re-review.

**Specification and code-quality verdict: PASS. No open Critical, Important, or Minor findings.** This is the final code-correctness review for Tasks 1–4; Task 6 capacity A/B acceptance remains pending.

## Strengths

- **One immutable committed projection, with the two read guarantees kept distinct.** `backend/app/services/market_read_state.py:10` and `backend/app/services/trading_prices.py:85` retain only frozen ordinary values, rather than ORM objects or mutable writer lists. Covered context/version/price reads acquire their complete shared GATE batch before loading/copying. Fully warm publisher and pure FX reads can use the previous complete committed state immediately. `credit/trading_context.py` closes account-read sessions before that GATE wait. Cold loading follows GATE → per-key load lock → fresh session, and installation itself is synchronous.
- **FX integration follows the actual transaction owners.** `market_read_state.py:69` binds staged Pair objects to the outer transaction; capture consumes only that transaction's final values. Spot/PvE, net multileg, borrowed buy, short open/cover, trigger orders, liquidity and admin commit owners install before their market GATE exits and before account notifications. The local liquidation context at `credit/execution.py:769` preserves successful commit/install/notification even when the body returns early. Replay branches do not replace the latest state with an old receipt. Repository searches and call-site inspection found no uncovered supported Pair mutation in this branch's scope.
- **Failure and precision handling match the approved narrow design.** `market_read_state.py:93` invalidates all captured keys on installation failure and logs that the database committed; uncertain commit/cancellation also invalidates. Pre-commit flush/capture failures propagate. Admin create/update normalize through the existing database refresh before capture, preserving PostgreSQL Numeric scale without duplicating rounding logic. Creation retains its explicit no-market-GATE exception; a late missing cold query cannot remove the installed new key. Deletion invalidates after successful or uncertain commit.
- **LMSR reuses its existing authority and recovery lifecycle.** `market_writer.py:175`, `:191`, `:217`, and `:387` cover install/register/reload/apply. Projection installation finishes within the original write GATE, so an early account hint cannot let a covered read observe pre-apply q. Decimal q and the existing float b are retained; reload copies the complete pricing source. Failure invalidates before releasing the GATE, and unloaded/settled markets can still read the authoritative database. Startup resets/prewarms before producers; ownership/read-only predicates prevent another process from trusting a stale local projection.
- **Public SSE keeps its history proof without acquiring GATE under a row lock.** `fx/trading.py:210` selects `pool_version FOR SHARE`, reuses eligible memory only on equality, otherwise reads a fresh Pair in the same session. It never installs that fallback. Both `api/v1/fx_stream.py:80` and `fx/market_data.py:246` pass the resulting state explicitly. The initial lock-unavailable path keeps quote availability while declining history readiness. Existing anchor order, precise volume, generation checks, event coverage and row-lock release before network delivery remain intact.
- **Shared serialization stays within existing interfaces.** `trading_prices.py:247` reads each active market once and dumps each changed group once per round, then filters by subscriber dependencies. The existing queue and encoder do not mutate those shared dictionaries. No schema, dependency, account cache, extra writer queue, recovery state machine, or AMM/ledger/risk change was introduced.

## Issues

### Critical (must fix)

None identified.

### Important (should fix)

None identified.

### Minor (nice to have)

None raised. I did not turn hypothetical faults or stylistic preferences into additional requirements.

## Verification evidence and test quality

This review ran **no tests, services, browser interactions, or load generation**, as requested. Results below are supplied execution evidence, inspected alongside the relevant assertions and fixtures:

- Task 1: 8 focused/context tests and 12 startup tests passed.
- Task 2: 244 related FX/credit/read-state tests passed; 12 real PostgreSQL context/order tests passed.
- Task 3: 42 related writer/context/liquidation/creation tests passed; the final affected admin file passed 6 tests after its test-only addition.
- Task 4: 21 public/context/stream tests, 41 targeted public/service/session tests, and 6 PostgreSQL history tests passed.
- Final concentration at `e50b0b3`: **315 passed, 2 failed**. Both failures were the subsequently resolved Registry test-lifecycle leak. At `330f4e6`, the unchanged minimal reproduction passed **3 tests**, and the affected fixture selection passed **52 tests**. The final complete 317-case command was **not rerun**, so this report does not claim a fresh 317/317 result.
- Final real PostgreSQL union: **18 passed**, no skips, on production source identical to `330f4e6`; that final commit changes only the shared test fixture.

The fixture fix resets Registry state and eligibility at the same boundary that replaces the disposable schema. It addresses the observed wrong-database cold load without weakening assertions or changing production behavior. The independent scoped re-review passed; its previous P2 is resolved.

Tests primarily extend existing real service scenarios and compare snapshots with refreshed committed rows. Focused additions cover the new failure boundary, replay preservation, multi-leg final state, Decimal representation, immutable ownership, lost wake recovery, and subscriber filtering. Real PostgreSQL tests exercise commit-to-install ordering, cold-read/write ordering, database fee normalization, and both public SSE consumers completing while the writer still holds GATE. Existing history assertions were retained. No broad new testing framework or redundant exhaustive failure matrix was added.

## Recommendations

Proceed with the already approved Task 6 A/B using the fixed baseline and identical instrumentation. Report market-input SQL separately from the retained account/history and public row-version proof queries, and report actual completed workload and stopping boundaries. Preserve the distinction between correctness evidence and measured performance. No additional correctness rerun or new permanent test is requested by this review.

## Declined to judge

- **Capacity, CPU, latency, successful throughput, and residual lock-wait improvements:** the planned A/B has not run; static SQL elimination and passing tests cannot establish those outcomes.
- **Production worker topology and deployment readiness:** this review did not inspect or change running production configuration. The code deliberately provides process-local snapshots and database fallback for read-only/non-owner instances; deployment verification remains the planned operational step.
- **Automatic coherence after unsupported direct SQL maintenance:** the approved contract requires reload/restart; cross-process synchronization and maintenance polling are explicitly outside this migration.
- **Public SSE consumer task scheduling/cancellation optimization and removal of history row-lock waits:** these paths were deliberately retained by the approved scope and require separate evidence/authorization.
- **A fresh exhaustive audit of unchanged AMM, financial ledger, risk and history algorithms:** the review checked that integration preserves their existing authority and protocol, but this migration is not a revalidation of every unchanged business rule.

## Assessment

**Ready to merge? Yes, on code correctness and scope.** No implementation fix is required by this review. The integrated commit/read boundaries follow the approved design and the supplied targeted/real-PostgreSQL evidence supports them; final plan completion and performance claims still require the pending Task 6 A/B. This assessment does not authorize pushing or deployment.

Only this requested ignored review artifact was written. Tracked source, index, HEAD and branch state were not modified.
