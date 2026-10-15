# Task 3 report

Implemented controlled real FX spot execution through the existing ordinary spot transaction and `_write_spot_ledger`. No net execution, borrowing, shadow balances, engine, HTTP privilege parameters, UI, schema changes or recovery mechanism.

## Contracts for Task 5 / management

`app.services.pve_fx.executor`:

- `execute_bot_order(profile_id: int, decision: FxDecision, frame: FxTickFrame) -> FxExecutionResult`: independent sessions; internally saves observation state first, then performs ordinary spot execution with atomic `on_fill` callback. Returned fill contains original intent in `requested_amount`, actual inputs/outputs and actual before/after cash/foreign. Expected rejection returns `fill=None`, committed observation state and a rejection string. Unexpected infrastructure exceptions propagate; there is no retry flow.
- `save_observation_state(profile_id: int, decision: FxDecision, frame: FxTickFrame) -> dict`: independent committed observation transaction, returns current persisted state. Engine calls this for waiting decisions; for an order call only `execute_bot_order` to avoid duplicate saves. `prepare_tick` alone does not save anything; save the arrived/observed `decide` result. Only matching active profile/pair/current episode can merge peak_return (monotonic maximum) and closing_reason (first reason wins). Episode identity/financial state must match direction, entry_tick, remaining and basis. Does not persist pending/pending_tick, quantity, cost, realized PnL or last action. Paused profiles and ended/changed episodes remain untouched. Locks pair -> User -> wallet -> treasury -> profile, under existing pair gate.
- `liquidate_fx_bot(profile_id: int, min_out: Decimal, operator_id: int) -> FxExecutionResult`: one ordinary controlled spot batch; requires paused profile and active superuser. Closing an episode uses its actual remaining quantity and correct direction (negative episode buys back). No episode sells owned wallet. The same pool limit applies, so large positions may require another explicit manual call. Actual fills update remaining/basis/PnL; completion clears episode; profile stays paused. Does not bypass FX enabled or product status rules.

`trading.execute_trade_in_session` preserves existing positional arguments/default behavior, with internal keywords `amount_mode='input'`, `bot_profile_id=None`, `bot_purpose='scheduled'`, `bot_operator_id=None`, `bot_reference_price=None`. Existing `credit_deps`/`borrow_amount` remain. Bot borrowing is rejected. `bot_reference_price` supplies the fixed tick reference for inventory budget clipping; it is required only for bot input-mode buys.

`trading.execute_trade` preserves existing positional arguments and adds the same internal bot/mode/reference keywords and `before_commit: Callable[[AsyncSession,FxTradeExecution],Awaitable[None]]|None`. Callback runs only for new executions after shared ledger/audit/event writes and before shared commit/account hints. Replay returns without callback. Full account publication, market-data notification, refresh and bounded publisher enqueue remain intact.

Both controlled purposes validate active real bot User, FX profile/User/pair binding, profile status, no cash or independent foreign principal/interest debt. Scheduled also requires total and pair bot flags; admin uses paused plus verified superuser. Human TOS remains unchanged; public HTTP still rejects bots including replay attempts.

Foreign-out uses fresh locked quote_buy_exact_out. Pool/wallet clipping and affordability occur under the same locks. A budget quote with available cash only determines affordable foreign quantity; execution stays exact-out. Original foreign target persists in existing FxTrade.requested_foreign_amount and audit payload, distinguishing replay mode/amount; actual output may be smaller. As confirmed by controller, input-mode legacy matching remains unchanged when clipping changes actual input; no extra request fields/retry mechanism introduced.

## Verification

Final focused command from worktree backend, outside sandbox against conftest disposable SQLite DB:

`venv/bin/python -m pytest tests/test_fx_pve_execution.py tests/test_fx_trading_service.py tests/test_fx_trading_in_session.py tests/test_fx_publication_e2e.py -q`

**54 passed in 9.16s** (11 bot-specific execution cases, 11 existing service, 28 existing in-session, 4 publication). Initial focused run before final partial/admin and publisher examples: 52 passed in 8.65s. A prior command attempted the test run before creating its file at the correct relative path and collected zero tests; corrected immediately. No product test failure was suppressed.

Evidence includes actual custom initial allocation -> controlled buy/sell -> audit_replay.fold(check=True) with no mismatches and compare_with_live empty; real ASGI ordinary human BUY/SELL vs bot snapshots for cash, wallet, cost, pool, treasury, trades and audit economics; HTTP bot rejection; disabled/paused/wrong pair/debt/flags rejection; exact six-place output; affordability clipping with original target preserved; replay without callback and mode conflict; callback intentionally raising ValueError leaves balances, trade count and last_trade_at unchanged while peak/closing reason remain committed; real partial episode size/basis changes and paused superuser close; bot commit reaching existing broker public frame and pool event.

`git diff --check`: exit 0.

## Self-review / limits

Reviewed owned changes and lock ordering, replay identity, callback rollback, profile pause/closed episode preservation, fee and cost sharing, no research imports and unchanged HTTP schemas. No production migration, grant, trade, bot gate activation or deployment. Tests use SQLite, so PostgreSQL row-lock scheduling itself was not exercised. Foreign debt admission is implemented using principal OR interest across all pairs; the focused suite tests cash debt while existing short tests were not expanded. No browser automation or full suite beyond requested relevant checks.
