# Unified user management implementation plan

User approved the unified user-management route and the necessary administrator API additions, requesting continuous implementation.

Goal: Search/select users, inspect holdings/recent transactions/admin history, and preview/execute explicit batch account operations from one page. Retain existing cash/debt/title operations.

Architecture: Vue/Naive UI stays unchanged. Add bounded read helpers and admin routes. Financial close operations use existing FX/LMSR writers, settlement kernels, liquidation run/action records and audit events; no new tables, queues, credentials or deployment mechanism.

## Constraints
- No production account operations while building or validating this feature.
- No automatic funding, writeoff, automatic ban-triggered liquidation, recovery mechanism or background job.
- Batch actions apply to an explicit bounded ID set; exclude self, administrators and official bots from new batch account/close actions.
- Preview close sequence with rolling reserves, fees and accrued short interest. Execution requotes under writer locks; insufficient cash or a blocked market reports a failure without partial cover of that group.
- Report per-user success/failure, including committed legs if a later market fails. Existing ordinary cash repayment semantics are retained.
- Keep all existing management functions available; old batch URL redirects to the unified management tools. Default UI verification is build/type-check and screenshots only, no browser interactions.
- Add concise backend behavior tests for data exposure/auth, explicit targets, full settlement, blocked/insufficient cash and audit consistency. Do not add style/layout tests.

## Shared API contract
GET /api/v1/admin/users keeps array response, adds optional user_ids (comma-separated positive IDs), is_active, has_positions, has_shorts, include_bots; q/offset/limit retained. Rows add spot_count, short_count, lmsr_count, registered_at, last_trade_at, is_bot.
GET /api/v1/admin/users/{id}/activity returns {positions:[{product,group_id,market_name,asset_name,kind,quantity,interest,cost_basis}],transactions:[{id,product,created_at,market_name,side,purpose,quantity,gold,source}],history:[{id,created_at,event_type,operator_user_id,reason}],registered_at,last_trade_at}. Financial fields are JSON numbers; interest is included separately from short principal.
POST /api/v1/admin/users/batch/accounts takes {user_ids:number[], action:'ban'|'unban'|'close'|'ban_and_close', reason:string, dry_run:boolean=true}. Returns {dry_run,items:[{user_id,username,status:'ready'|'success'|'failed'|'skipped',message,cash_before?,cash_after?,trades?:[{product,group_id,side,quantity,gold}]}],price_impacts:[{pair_id,currency_name,price_before,price_after}]}. All preview items shown; execution targets exact confirmed ready IDs only. Max 100 IDs.
Existing UserFilter adds user_ids?:number[] so cash/amnesty can operate on explicit selected users.

## Tasks
- [x] Read side: bounded list enrichment, activity view, real data tests.
- [x] Batch/admin close: authenticated routes, explicit batch account ops, sequential preview and writer-owned full close, behavioral tests.
- [x] Frontend: unified searchable/selectable table, detail tabs, batch preview/results and existing cash/debt tools. Update sidebar/old route/review links.
- [x] Verification: focused backend tests, frontend build and relevant existing tests, one scoped review and repair. No deployment until changes are concrete and validated.


## Delivery and validation

Implemented in the existing checkout, preserving unrelated untracked files. No new dependency, database migration, background queue or deployment change. The new operations require administrator authentication and explicit IDs/reason. Admin close cancels existing waiting trigger orders in their owning writer and retains any residual gold borrowing unless normal auto repayment applies. Product groups commit sequentially; already committed groups are included in a later failure result.

Checks actually run:

```sh
backend/venv/bin/python -m pytest backend/tests/test_admin_user_hub_api.py backend/tests/test_admin_user_reads.py backend/tests/test_admin_user_batch.py backend/tests/test_batch_adjust_cash.py backend/tests/test_admin_amnesty.py backend/tests/test_loan_admin.py backend/tests/test_credit_wp6b_admin.py backend/tests/test_writer_admin_ops.py -q
# 71 passed, 14.34s
cd thccb-frontend
npm run build
# vue-tsc + Vite passed; existing chunk-size/mixed-import/empty-chunk warnings remain
```

Additional batch/writer/LMSR-fee checks passed (16 cases before the last small cancellation-helper/scoped-failure refinements); the final batch module passed all 7 cases and is included in the consolidated 71 above. A real short-open -> interest -> admin cover test verifies audit replay and live tables agree.

Desktop (1440), mobile (390) and direct-link detail screenshots were captured with local fake users and intercepted read requests only. All three rendered with no page errors or document horizontal overflow; no button clicks, form fills or browser interaction scenarios were run. Screenshot/temporary review artifacts are in `/tmp/thccb-admin-hub/`. Final independent review found no material issues.

Implementation and validation did not perform production account operations or schema migrations. The user subsequently authorized publishing through the existing CI and Coolify workflow.
