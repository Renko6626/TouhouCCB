# Task 4b independent review

**Decision: approved, with one optional documentation cleanup.** No Critical or Important finding was identified in this bounded Task4b review. Approval applies to `5978b3b..b413408`, including production `6eb1e8d` and the peer/final test changes; it is not final-branch or performance acceptance.

## Findings

- **Critical:** none.
- **Important:** none.
- **Minor — stale description of risk snapshot completion.** `backend/app/services/credit/risk.py:622` still says missing snapshots are filled while valuing collateral. The implementation deliberately does the opposite: `_ensure_snapshots` returns only the fixed prepared capture and an absent required record causes an incomplete/unknown decision. Update this docstring when convenient so it documents the new financial invariant. The existing `backend/app/services/fx/net_trading.py:262` caller-contract docstring also still refers to gates. These are documentation issues; no execution change or additional test is needed.

## Spec and financial correctness

The review read the approved plan, Task4 brief/preflight, implementation and peer-test reports, and the supplied diff in bounded production/financial-test passes. Targeted final-file reads were used to resolve lock, simulation and valuation context; no general codebase crawl was performed.

- `risk.py:462` constructs FX asset and cover inputs from the same immutable record and LMSR q, outcome order, b, status and close time from its record. `discover_dependencies` refreshes the own User and reads the full positive position/wallet and live-short ledger before the synchronous capture. Global fee/rate reads remain separate. There is no foreign SQL quote supplement or mutable writer q in this execution path.
- The inspected FX spot/financing, net, short and LMSR admission handlers prepare final dependencies after their real User lock and before applying the financial simulation. FX retains Pair → User ordering; each writer's transaction retains the User lock until commit. The old discovery values no longer authorize the execution. Account-locked preparation is an explicit internal caller contract, and the production call sites inspected satisfy it.
- Standalone borrowing (`api/v1/loan.py:82`) and financed FX buying (`fx/trading.py:489`) compute effective old gold debt at T and normalize the risk accrual timestamp before adding new principal. The new path does not charge that pending interest again. Target absolute short debt is constructed from current locked rows; the legacy prefetch preparation rejects rebasing an absolute short simulation when its own ledger has changed (`risk.py:728`).
- Missing published short prices cannot remove the obligation from the no-debt check: `DependencySet.short_debts` is independent of available market records, `_has_short_obligation` consults it, and `_short_risk_after` produces unknown K for a missing required record. Required missing positive-asset records also deny margin admission. No healthy-SQL fallback was introduced.
- Actual target reserves/q still come from the existing locked transaction or its own serialized LMSR actor. FX min-out, explicit net economic versions, replay identities, own-short direction restrictions, ownership guards, and the financial ledger writes remain in place. Other markets' later publication does not invalidate an already prepared account check.
- Liquidation now supplies its fixed capture to the existing valuation formulas. The prepared branch in `valuation.py:279` reads fresh own amounts and static IDs/currency labels, without foreign quote columns. `execution.py:531` replaces only the actual target's quote inputs from its own transaction, and `finish_locked` retains the same captured foreign records after the action. The flush precedes those post-action reads. It does not publish the uncommitted target state. Selection/ranking and bounded retry policy remain intact.
- Generic GATE deletion leaves target actor ordering, OWNERSHIP and actual database financial locks in place. Production history-prefix/FOR SHARE behavior was not removed by this patch. The history test barrier is now scoped to the writer's commit instead of intercepting its reader's commit as well.

## Test changes and evidence

The new PG same-account case (`tests/pg/test_pg_credit_admission_contention.py:103`) controls the two real FX consumers at the User lock. It calls the actual risk function and asserts that the second consumer sees cash30, debt70, version2 and the exact first holding, then checks final persisted cash/debt/wallet/version. It is meaningful evidence against duplicate financing from an old account basis, rather than a lock-spy-only assertion.

The stale-prefetch PG case (`:162`) checks cleared FX/LMSR collateral plus newly committed gold and foreign debt, actual denial and cover cost, and unchanged final funds/version. The unit fixed-capture tests verify numerical A/K results when SQL and a later publication differ from the captured input, including a post-liquidation target update with unchanged foreign capture. The missing published-short case denies with unknown equity/K despite a valid SQL pair.

Obsolete GATE-only tests/imports were removed. Adapted real financial tests retain observable balances, debt, interest, ledger/replay and version checks. The repayment race changes only the approved baseline fixture from repay-all to repay200. The 100-user mixed scan admits first-wave retry exhaustion only under the approved unfrozen-market semantics, performs exactly one existing next-pass call per affected user after quiescence, and retains final100 unique actions/users/events and both FX/LMSR coverage. It does not weaken those final assertions, freeze prices, add unbounded test retries or alter production retry policy. The seed fixture is opt-in and wraps module seed helpers; it is not an execution hook or production fallback.

Evidence was inspected, not rerun: the reported concentrated unit slice has passing evidence for all354 cases via its main run plus localized fixes; the seven-module real-PG slice has passing evidence for all45 cases via44 passing plus the corrected stale-prefetch node. Neither final combined command was repeated solely after the reported assertion/fixture corrections. Reported compileall, diff checking and test collection passed; collection was not a full repository test run.

## Limits

This reviewer ran no pytest suites, PostgreSQL operations/services, browser flows or performance workloads, and changed no production/test/git state. Only this ignored review artifact was written. Task4a ordinary-read approval was treated as established context, with this pass checking its interaction with actual execution risk and GATE removal. The previously deferred Task1 recovery-stop future timing concern remains for final-branch review and was not reclassified as a Task4 regression. No new architecture or defensive mechanism is requested.
