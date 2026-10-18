# Task 4 test-only slice report

Scope: only backend/tests/test_credit_wp6b_admin.py, test_credit_wp6b_consumption.py, test_credit_wp6a2_lmsr.py, and test_credit_wp6a1_admission.py. Production, conftest, shared fixtures, PG, services, browser, load tests, and full suite were not changed or run by this worker.

Pre-commit HEAD: `5978b3be65e45034b9588b252a5ffdd29aa99e4b`.

## Changes and self-review

- Opt into the already provided `published_markets` fixture. Direct SQL additions made after seed helpers explicitly call the existing `tests.conftest.publish_seeded_market_records` helper. This is fixture publication of committed inputs, with no execution fallback.
- Remove obsolete GATE imports and holder/release assertions. The one pure test asserting writer submission is rejected while holding a GATE is removed because that coordination mechanism no longer exists.
- Replace gate-spy cases with actual discovered holdings/short debt/published reserve inputs passed through the real risk function, plus actual accepted/rejected money, debt, q, version, and ledger results.
- The old FX stale-prefetch retry loop case now checks that an account cash change committed before User lock prevents a purchase and preserves debt/version and the absence of trade rows. Its clean rollback assertion remains.
- The LMSR changing-account case injects the account write before User lock, rather than writing a second connection after the risk function is already holding the account lock. It checks fresh cash/version, full external collateral, real target post-state, and final cash/debt/q/version.
- Admin new-short-before-lock retains exact cash/version and single ledger-entry assertions and now also verifies the actual new short principal and committed pricing input reach risk admission.
- Consumption automatic commit is asserted through persisted cash/version and exhausted code availability, replacing gate lifetime assertions.
- Existing financial, frozen-user/operator, interest, audit replay, writeoff, replay/idempotency, and loan result assertions remain. No constants or source-string assertions, new fixture frameworks, or redundant permanent tests were added.

## Commands and results

Interpreter: `/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python`.

1. From `backend`:

   `/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest tests/test_credit_wp6b_admin.py tests/test_credit_wp6b_consumption.py tests/test_credit_wp6a2_lmsr.py tests/test_credit_wp6a1_admission.py -q`

   Result: **57 passed, 4 failed in 9.38s**. All four failures were the newly written helper import using `conftest` rather than the package-qualified `tests.conftest`.

2. After correcting the four imports, from `backend`:

   `/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest tests/test_credit_wp6b_admin.py::test_unified_debit_prices_short_appearing_before_user_lock tests/test_credit_wp6a2_lmsr.py::test_buy_uses_fresh_account_and_full_published_collateral tests/test_credit_wp6a2_lmsr.py::test_fx_collateral_can_fund_lmsr_admission tests/test_credit_wp6a1_admission.py::test_fx_buy_values_all_published_collateral -q`

   Result: **4 passed in 0.80s**.

3. After final self-review strengthened three affected cases, an invocation from repository root with `tests/...` paths returned **no tests ran, exit 4**, because those paths are relative to backend. No product/test failure was reported.

4. Corrected to `backend`:

   `/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest tests/test_credit_wp6b_admin.py::test_unified_debit_prices_short_appearing_before_user_lock tests/test_credit_wp6a2_lmsr.py::test_fx_collateral_can_fund_lmsr_admission tests/test_credit_wp6a1_admission.py::test_fx_buy_uses_cash_refreshed_at_user_lock -q`

   Result: **3 passed in 0.63s**.

5. From repository root:

   `git diff --check -- backend/tests/test_credit_wp6b_admin.py backend/tests/test_credit_wp6b_consumption.py backend/tests/test_credit_wp6a2_lmsr.py backend/tests/test_credit_wp6a1_admission.py`

   Result: **passed**.

All 61 remaining tests in the four-file slice have passing evidence, with only affected failures/edits rerun. This is scoped test evidence, not whole-migration completion or PG contention evidence. Other workers' production and fixture edits were present during verification and were not staged by this worker.

Scoped implementation commit: `6b69f4e` (`test: align credit admission with committed market snapshots`), containing exactly the four owned test files. The report is left unstaged for the controller.
