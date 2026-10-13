# FX 到价挂单最终审阅（2026-10-14）

Final status: **Ready. No remaining C/I/M findings.** Sol medium reviewed Task6 and the whole branch, then approved the narrow reason-copy correction `bfaeaa7`. The initial finding and scoped resolution below preserve the review evidence; this is not a further review wave.

Reviewed base `de3c7929c459c0f8c121ad1a2262e36c16cc72eb` through `f45dabfaf197ea211cc1a38b9eb7e46bdb72371a`, final review package, confirmed spec/plan, constraints/progress and Task6 report; consulted Task2/3/5 evidence for migration, transaction and actual component submission limits. Read-only production review; no tests rerun, browser interactions, code edits or delegation.

Verdict: **Needs fixes — one local M finding. No C/I findings.**

## Finding

**M — Known terminal failure reasons disappear behind generic player text.** `thccb-frontend/src/api/fx.ts:69` maps `no short position to cover`, but actual common close-short execution returns `no outstanding short to cover` (`backend/app/services/fx/shorts.py:874`, also :894). `describeFxOrderFailure` (:707–708) therefore shows a generic failure in both the result notification and history when a player has already closed the authorized short. This misses the confirmed explicit “已无对应持仓” explanation. The same dictionary omits the new `auto borrow limit exceeded` (`net_trading.py:136`) and `short close quantity is too small` (`shorts.py:898`) reasons, concealing the directly actionable authorized-limit/proportion failures.

Minimal remedy: map the actual three known reason strings to specific natural Chinese text (replace the unused no-short key, or retain it as an alias). Preserve the generic fallback for unknown/internal reasons. No backend behavior, new mechanism, API, algorithm or permanent test project is needed; a small check of the existing pure formatter and normal relevant static check are sufficient.

## Core assessment

- Web execution retains its existing version/quote checks and transaction wrapper while calling the extracted financial core. Order execution resolves financing after cover and lock release, and fixed-direction close quantities from locked current state; full short cover settles interest at the same execution time. Existing ledger helpers remain shared.
- Creation counts all pairs under the User row lock; private listing/cancellation bind ownership. Execution locks GATES → Pair → User → order and rechecks waiting. Funds, execution receipt and succeeded result commit together. Explicit failure rolls back funds before separately rechecking/recording failed; cancellation cannot be overwritten. Fixed request key/receipt checks stop inconsistent or unreadable outcomes without introducing recovery state.
- Scheduler uses a completed batch read, six-place public pool-ratio observations, inclusive conditions and sequential ID processing with per-order transactions. Paused/FX-off candidates terminate; permanent unavailability ends waiting orders. Default/configurable interval, nonoverlap, ownership startup and shutdown waiting match the approved route.
- Private postcommit result publication and account refresh use existing queues; account reason suppresses only the duplicate generic toast. Existing identity guards reject late old-account frames, and component loads clear on identity changes. Decimal input handlers now preserve raw strings into actual submit/API arguments.
- Incremental schema, history-based deletion refusal, child-first season reset and baseline/head integration match the approved design. Task6 docs accurately distinguish local implementation, final-review pending status, passing checks, existing lint/build warnings and unverified production operations.

## Evidence and limits

Accepted parent-read final evidence: backend compile/import pass; 1456 passed, 4 skipped, 44 deselected; frontend 252 passed; latest typecheck/build pass; actual disposable PG 3 passed plus cross-pair slot case 1 passed. Read-only ESLint retains 40 errors in unchanged files. Component screenshots prove layout only; temporary actual-SFC submit proof used mocked API writes. Full live browser/login/transactions, production migration/deployment and capacity were not verified. No additional theoretical risk matrix or mechanism is requested.

## Scoped re-review — bfaeaa7

Final verdict: **Ready. No remaining C/I/M findings.** Reviewed `review-f45dabf..bfaeaa7.diff` and `final-fix-report.md` without rerunning checks or expanding review scope. The three exact production reason strings now map to specific, natural Chinese explanations; legacy alias and unknown/null fallback are preserved. Only dictionary entries changed, so the notification/history consumers and financial behavior remain intact. Accepted reported actual-exported-formatter six-call runtime pass, typecheck pass and whitespace check pass.

The validation record also correctly states that the explicitly supplied temporary SQLite URL is retained by conftest's `setdefault`; the inaccurate PID isolation claim is removed. No backend changes or new functionality accompany this fix. Earlier verification limits and lack of publishing/deployment authority remain as recorded.
