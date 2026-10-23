# ASGI middleware implementation

Isolated worktree: `/tmp/sunyunbo/thccb-asgi-20261023/candidate`
Branch: `perf/asgi-middleware-20261023`
Baseline A: `65897eb` (4 Hz SSE aggregation)
B: `1f78cd0` — access policy/logging uses direct ASGI; risk logging remains BaseHTTPMiddleware.
C: `9224cec6a2070ae7a8d0a071a7a223f903f7e190` — access and risk logging both use direct ASGI.

Only `backend/app/main.py`, new `backend/app/core/http_middleware.py`, and new `backend/tests/test_http_middleware.py` were changed. No changes to ownership, cadence, SQL, schema, framework, configuration, risk_log internals, deployment, or load-client behavior. No subagents, load runs, production access, push, or deployment performed by this implementer.

## Behavior

`main.py` registers the two production classes with public `app.add_middleware`. The outer-to-inner order remains ServerError → risk → access → CORS → exception/router. Dependencies point from main into the dedicated middleware module; the module does not import main.

Access retains the existing ownership/read-only guard and calls the real `OWNERSHIP.require_writes`. Its early 503 retains baseline behavior: no access log, no API cache headers, and no inner CORS processing. Other API responses receive no-store/Pragma, including access-log-skipped API stream paths. Existing skip prefixes and warning severity for 5xx are preserved. Header edits use MutableHeaders so repeated Set-Cookie survives.

Both wrappers directly forward non-HTTP traffic, receive, and body messages. Risk bypasses safe methods/non-API paths without creating Request. For mutating API requests it creates Request(scope), never consumes body, and calls the unchanged real risk_log.record.

Response status and header-ready duration are recorded when the send wrapper receives http.response.start, before forwarding that message. The response-start flag is set first. Exceptions before headers retain EXC access / 500 risk logging and propagate; body, background, or send exceptions after headers propagate without a second log. CancelledError propagates without being converted to 500; cancellation after headers retains the already emitted response record. No buffering, task group, recovery queue, retries, or new state outside the individual call was added.

## Verification

All pytest commands ran from candidate/backend with `env -u DATABASE_URL` and interpreter `/tmp/sunyunbo/thccb-pr23-review-20261023/review-venv/bin/python`.

B initial command:

```
env -u DATABASE_URL /tmp/sunyunbo/thccb-pr23-review-20261023/review-venv/bin/python -m pytest tests/test_http_middleware.py tests/test_credit_final_guards.py tests/test_api_stream_namespace.py tests/test_fx_stream.py tests/test_trading_stream.py tests/test_history_endpoint.py -q
```

Result: 43 passed in 8.18s. After adjusting log timing to occur before await send, the affected `tests/test_http_middleware.py -q` suite passed: 6 passed in 0.77s. B then committed.

C used the same concentrated command. Result: 50 passed, 1 failed in 8.53s. The failure was the new readonly fixture setting `_reason` but omitting `_writes_enabled=False`; real require_writes correctly allowed the still-enabled writer. Fixed the fixture using the existing ownership test pattern. Also added one response-after-headers cancellation parameter. Final affected command:

```
env -u DATABASE_URL /tmp/sunyunbo/thccb-pr23-review-20261023/review-venv/bin/python -m pytest tests/test_http_middleware.py -q
```

Result: 15 passed in 1.41s. The 37 existing guard/stream/history cases had all passed in the concentrated C run; production code did not change after that run. `git diff --check` passed for both commits. Worktree clean after C commit.

New behavior coverage uses the actual production middleware classes, a FastAPI route, and actual risk_log.record with isolated temporary log files. It verifies body preservation, two cookies, CORS, API/history cache policy, background execution, pre/post-header failure, readonly 503 exactly once and before send, send/background failure, disconnect and cancellation boundaries, bypass paths, and an actual risk-directory I/O failure returning the original business response. Authenticated risk DB/bot lookup behavior is unchanged and was not separately exercised. Raw ASGI fixtures are used for exception/cancellation/send boundaries; no browser interaction or E2E run occurred.

## Compatibility / scope limits

The historical `loadtest/fx_capacity_server.py` asgi_cadence8/split_reads prototype branches reference the removed main.log_requests/_LOG_SKIP_PREFIXES symbols and should run against their historical checkouts. The current-only path does not rely on those symbols. No duplicate inactive policy helpers were retained for the old prototype. `history.py` has an explanatory historical helper-name reference only; its executable behavior is unaffected.

Performance/PG sampling and independent review belong to the parent controller. These commits are experimental candidates; nothing was applied to the main user worktree.
