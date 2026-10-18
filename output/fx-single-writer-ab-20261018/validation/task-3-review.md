# Task 3 review — LMSR lifecycle and publish-before-notify

Reviewed fixed commit range `070ff4d..473498e` using the supplied diff, task brief/report, approved migration plan and LMSR audit. Focused unchanged-code inspection used `git show 473498e:...` and a commit-scoped production call-site search. Concurrent FX/main/conftest changes are outside this review.

## Findings and decision

No actionable findings in the reviewed Task 3 changes. **Task 3 approved at `473498e`.** This decision covers the LMSR lifecycle/notification task only; it does not approve the complete migration or other agents' changes.

## Spec and quality checks

- Existing-ID `register_market` and external `reload_state` submit the private maintenance command to the existing market queue. The maintenance branch precedes the unavailable economic-write check. Consumer exception recovery calls `_reload_state` directly, avoiding self-submission. New-ID bootstrap installs the initial snapshot before exposing the state/queue; the transitional gate recheck prevents concurrent bootstrap replacement.
- Reload updates the existing `MarketState` object, retains `last_ring_trade_id`, and preserves the existing candle-flush/ring rebuild and unavailable/invalidation behavior. A queued economic command obtains state only when the consumer reaches it, after the preceding reload has finished.
- The transaction helper collects primitive account descriptors before commit, populates the yielded list after successful commit, and defaults to its previous immediate publication behavior. Every changed LMSR transaction passes the deferred list through its outcome, including settlement replay and liquidation early returns.
- `_execute_gated` finishes synchronous `_apply_outcome`/registry installation before the consumer publishes account/pricing hints. Publication precedes tick feeding, future completion and the next command. Installation failure bypasses publication and retains existing invalidation/reload handling.
- The production diff changes lifecycle and notification plumbing only. Financial calculations, User/Position locks and refresh behavior, ownership checks, economic versions, liquidation business-key replay and history merge logic are retained. Target/collateral gates remain intentionally pending Task 4.
- The revised existing account-hint test observes the committed/pre-install window and verifies the hint remains absent; existing failure coverage checks no hint escapes failed installation. The added lifecycle case uses production buy operations and checks final DB, writer and registry quantities for both registration and reload.

## Evidence and limits

Read-only review of production and tests; no existing suites were rerun, no services/browser flows were started, and no production/git state was mutated. Only this review record was written.

The implementer's report records 34 passing tests for writer/account/admin/ring suites and 24 passing tests for state/tick/liquidation/fee suites, plus a successful diff whitespace check and AST parsing. Those are reported implementation evidence, not independently rerun results. PostgreSQL concurrency and the later GATE/read/risk migration remain outside this task approval.
