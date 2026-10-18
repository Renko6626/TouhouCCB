# Task 2 report — remaining FX economic writers

Worktree `/tmp/sunyunbo/thccb-single-writer-20261018`, branch `perf/single-writer-markets-20261018`.

Owned commit `c53ccd3` (`feat: route remaining FX economic writes through pair actors`), immediate parent/review BASE `809ad3f991876b8869655433055d3834c3b679f2`. Task4a committed its independent ordinary-read changes before this commit; review only `809ad3f..c53ccd3` for Task2. Original approved baseline was `98e846e`; docs-only `4ce4081` and read-worker `809ad3f` are excluded from this Task2 code commit. Staged only the 16 owned production/test paths printed before committing. This report is an ignored local SDD artifact.

## Delivered transaction and queue ownership

All additional command DTOs are frozen/slots and carry IDs, Decimal, strings/bools, serialized request/state JSON and primitive tuples only. No sessions, ORM rows or producer callbacks cross the queue. Producer wrappers materialize IDs before closing request sessions; scheduler and liquidation discoveries close their sessions before submit. Financial handlers use fresh sessions and existing row locks. Transitional GATE remains. No architecture decision beyond the approved plan was needed.

| Entry | Final owner and preserved behavior |
| --- | --- |
| order create/cancel | `CreateOrder`/`CancelOrder` → writer transaction → no-commit session kernels. Per-user limit, ownership, terminal-state checks and locks retained; Pair queue orders controls against trigger execution. |
| order attempt/success/failure | `AttemptOrder` runs `_attempt_order_owned` inside the consumer. Existing bounded credit rediscovery/retry and `_in_session` financial instruction kernels retained. Money + receipt + order terminal state commit together, strict writer install precedes account/market hints while target GATE remains held. Failed money transaction rolls back before the separate original failure-record transaction; that failure remains consumer-owned and rechecks order/receipt under its target GATE. |
| permanently unavailable order | `FailOrder` invokes no-commit locked failure kernel, with fresh writer session/commit. Scheduler only discovers candidates/submits commands and later reads committed notification payloads. This means archived/closed pairs, not a fallback bypass for a broken actor. |
| FX liquidation sell/cover | Producer discovers expected user version/group IDs and target/role, closes discovery session, then submits `LiquidateRound`. Consumer rediscovers, locks Pair → User, repeats version/group/selection checks and calls extracted `_execute_locked_round`. Preparation/run creation, action, sell or cover, repayment/interest, finish/freeze, economic versions, audit and events remain one transaction. Selection drift returns to producer's bounded retry; actor never waits for LMSR or another actor. Non-FX/cash-only dispatch remains on existing path. |
| fund/withdraw | `Liquidity` command → original kernels with internal commits removed → pool/treasury/audit/pool event one transaction → strict commit/install → pricing + liquidity publication. Existing treasury/reserve rejection rules remain. |
| admin patch/archive/delete | `AdminPair` command → handler acquires existing capacity asyncio/PG protection before Pair lock → original no-commit validation/mutation kernels. Existing short/holding/history guards retained. Prior clean-request guard retained for these routes. PATCH/archive install synchronously after commit. Delete commits then marks actor deleted/unavailable, invalidates Registry and completes every queued future with 404 before response/next command. Later submit/reload/register for that ID returns 404. No new table, durable tombstone or recovery system. |
| new pair | Existing capacity-protected creation transaction persists pair/treasury/audit/initial pool event. Response DTO copied before commit, producer session closed, then explicit `register_pair` bootstrap installs state before successful creation response. No production lazy actor creation. |
| PvE observation | `ObserveBot` command performs original Pair/User/wallet/treasury/profile lock sequence and episode/peak/closing-reason checks in a writer session. Observation and trade remain separate original transactions, with no locks between submissions. Existing Task1 BotTrade `_apply_fill_in_session` is unchanged, keeping on_fill/profile/last_trade_at with money. |
| admin FX short writeoff | `WriteoffShort` invokes extracted no-commit financial kernel. Pair/User/short locks, interest calculation, existing audit idempotency identity/replay/conflict rules, version/freeze semantics and zero minted repayment are retained. Writer commits before account hints. Expected AdminUserError is treated as business rejection by consumer, avoiding unnecessary recovery. |

`commit_and_install` is FX-specific, has no callback/transaction framework, and propagates installation failure. Remaining handlers do not use the old swallow-and-continue `commit_fx_changes` helper. Existing Task1 core Spot/Borrow/Net/Short/Bot commands were not reimplemented.

Account-only bot generation/funding/strategy lifecycle/death marking and account-only loan/interest operations remain independent per the approved plan. Market-data/candle consumers retain their own projection transactions; they do not mutate AMM money. Announcement/config paths remain independent. Offline reset/repair remains offline-only. Shared risk/GATE cleanup, ordinary read semantics and LMSR internals belong to later tasks, and were not changed by this commit.

## Tests and fixture adaptations

Three focused behavior tests fill queue-coverage gaps with real financial kernels and persisted assertions:

- funding blocks delete/trade/reload; funding snapshot is installed before liquidity hint; deletion drains queued trade/reload futures with 404, preserves deletion/funding audits and unchanged user cash/no trades, and refuses re-registration;
- queued cancel precedes trigger attempt, retaining cancelled terminal outcome and unchanged money/no trade;
- FX round blocks funding already during locked preparation; after release, action/repayment/account version, remaining wallet and trade/fund pool-event chain prove complete round then funding.

Existing money/history/idempotency/version/slippage assertions retained. Detached producer rows after wrapper `close()` are explicitly reloaded in the existing admin and treasury scenarios. Existing admin gate-through-commit spies now observe the fresh writer-session class rather than the old request session, preserving the original gate assertion.

Root test seam starts FX writer for explicit `register_pair` as well as submit, because ASGI service tests omit app lifespan. Its submit bootstrap calls the captured original register under the fixture lock, avoiding patched-wrapper lock reentry. Production startup remains explicit. Controller-requested minimal PG fixture preparation stops FX writer/resets Registry before schema replacement and during teardown; the existing short-concurrency owner fixture also binds FX writer, credit execution, admin service and Registry to its acquired PG owner. Database URL guards/session factory binding remain intact. PG was not executed.

## Exact verification evidence

Interpreter `/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python`; pytest and compileall cwd `backend`. Disposable SQLite only, no services/install/load/browser/PG actions.

```text
# Final combined production verification; a new test's hint spy was then corrected:
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest -q tests/test_fx_writer.py tests/test_fx_trigger_orders.py tests/test_admin_fx.py tests/test_admin_fx_multi_pair.py tests/test_fx_pve_execution.py tests/test_credit_execution.py tests/test_fx_short_liquidation.py tests/test_fx_short_audit_replay.py tests/test_credit_wp6b_admin.py
1 failed, 145 passed in 27.15s
```

The sole failure was new `test_liquidity_delete_and_pending_trade_share_one_queue`, expecting an account hint for funding. Funding has no account change and preserves its actual liquidity publication hint; the test now observes that real hook and checks installed pool version there. No production fix or assertion weakening was needed for this failure. Corrected test file:

```text
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest -q tests/test_fx_writer.py
5 passed in 0.95s

# After fixture-only captured-register reentry fix:
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest -q tests/test_fx_trigger_orders.py tests/test_admin_fx_multi_pair.py tests/test_fx_short_audit_replay.py
30 passed in 5.54s

/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m compileall -q app/services/fx/writer.py app/services/fx/writer_ops.py app/services/fx/orders.py app/services/fx/liquidity.py app/services/credit/execution.py app/services/pve_fx/executor.py app/services/admin_user_service.py app/api/v1/admin_fx.py tests/conftest.py tests/pg/conftest.py tests/pg/test_pg_fx_short_concurrency.py tests/test_fx_writer.py tests/test_credit_execution.py tests/test_admin_fx.py tests/test_admin_fx_multi_pair.py tests/test_fx_short_liquidation.py
exit 0

git diff --check
git diff --cached --check
exit 0
```

No warnings in these final runs. Earlier diagnostic runs exposed the omitted test-lifespan bootstrap, obsolete session spies/detached producer fixture identities, and the cash-only status return after extracting round execution; these were corrected before the final combined run. A new round-ordering fixture initially selected full liquidation with negative equity; its debt was set to a known partial-liquidation scenario, retaining explicit 450 wallet, repayment/action/version/history assertions.

## Self-review and remaining validation

Read approved full plan, Task1 report, original frozen commit-owner/locking audit, and all owned diffs before staging. Checked no submitting wrapper runs in a consumer; no producer User/Pair/order/capacity lock survives submit; all AMM mutations stage final snapshot; commit/install/hints complete before next command; creation closes before bootstrap; deletion drains queue and prevents reload/register resurrection; all original audit payloads and monetary kernels retained. The only financial extraction correction was cash-only committed status returning immediately, now covered by retained existing test.

No unreviewed architecture decision outstanding. Controller still needs independent review and scheduled PostgreSQL tests (real User-lock contention, order races, liquidation rollback/idempotency), then shared risk/GATE migration and broader final validation/performance comparison. SQLite success does not establish PG locking behavior. Task2 completion is not a claim that the full migration is deployable or finished. Task1 stop/recovery future timing remains controller's already noted review item; no new recovery mechanism was added.
