# SSE cadence A/B tools

Tools are prepared only. No database, application server, pilot, or formal benchmark was started by this worker.

## Entrypoints

```bash
PY=/tmp/sunyunbo/thccb-pr23-review-20261023/review-venv/bin/python
TOOLS=/data/sunyunbo/www/TouhouCCB/output/sse-cadence-ab-20261023
"$PY" "$TOOLS/driver.py" run \
  --repo /tmp/sunyunbo/thccb-sse-20261023/baseline \
  --label baseline --users 100 --rate 1 --seconds 40 \
  --database fxcapacity_sse_baseline_u100_r1_test \
  --pg-socket /tmp/sunyunbo/thccb-sse-20261023/pg-socket \
  --pg-port 55924 --pg-user ssebench \
  --output "$TOOLS/baseline-u100-r1"
"$PY" "$TOOLS/summarize.py" --root "$TOOLS"
```

Use the exact same driver and flags with repo `implementation`, label `candidate`, and a fresh database/output name for the matching cell. Matrix: users 100/200 × total offered rate 1/20/54 × baseline/candidate. Duration is 40 seconds plus the reused 10-second, rate-10 warmup. A pilot may use `--seconds 2` in an output directory outside the formal summary root (otherwise it duplicates a matrix cell). Do not run cells concurrently.

Root supplied local PostgreSQL socket/port/user above; no additional connection parameters are missing. Still required: root's approval to start after product review/tests, fresh database/output names for each cell, and confirmation that any pilot is no longer competing with product checks.

## Reuse and corrections

- Imports the selected worktree's `CapacityRunner`, `RoutedClient`, `Metrics`, `Runner`, `percentiles`, and existing `fx_capacity_server` instrumentation. Reuses the original seed/reconciliation logic locally with the corrections below. No product code is modified.
- The sampler accepts only `FX_CAPACITY_VARIANT=current`. No old cadence, ASGI, or split-read override is enabled. Existing main middleware and risk/ownership checks remain present. The SQLAlchemy path is already optimized; this experiment does not add read workers or change middleware.
- Original seed fixed users=100. New seed receives the actual `--users`; client validates token user count; metadata records actual users and 2× streams; integrity validates the database user count.
- Original SSE pool had only 200 connections. New stream pool is 400 for every cell. Owner write/quote pool stays 200 and context/history read pool stays 150, all independent HTTP pools on the same owner UDS.
- One active LT pair, 60% cash / 20% financed / 20% short, cross-zero roles, quotes, trades, refreshes, and polling are inherited unchanged.
- Public per-topic cap500 and per-IP cap200 already admit 200 users. Private AccountEvents has no total subscriber cap in inspected code. No admission override is applied or needed; run metadata records an empty override list.
- User initial cash is 10000 each. Gold physical expectation is 10000000 + users×10000; with treasury 20000000 + users×10000. For 200 users these are 12000000 and 22000000. Foreign expectations remain 10000000/20000000. Fee-zero defaults and default-interest ledger reconciliation are retained, including trade-leg/event equality.
- Database names must match `fxcapacity_*_test`; driver issues CREATE only, failing if a name exists. Database remains for inspection; no DROP/reset command exists. PostgreSQL and HTTP use local Unix sockets. Application secret is randomly generated for each run, held only in the child environment; tokens/manifest/private UDS directory are deleted in finally, including failed runs. Existing/unknown databases are never removed. Raw logs/statistics stay in the output folder.
- Server terminates in finally with a bounded graceful stop then kill fallback. HTTP client is a bounded subprocess. Migration, seed, server, client, requests, resources, visibility and integrity outputs are preserved. No token file is copied to output.

## Summary and limits

`summarize.py` reuses the previous resource aggregator and adds RSS peak, client CPU, actual matrix dimensions, missing/incomplete cells, abort reason and integrity status. `comparison.json` contains successful POST TPS within deadline (and completed-cycle TPS separately), POST/cycle P95/P99, per-result rejects, skipped/failed cycles, owner CPU/RSS/SQL/queue/handler/loop lag, connected public/private counts, SSE frames/payload bytes, client CPU/schedule lag, visibility latency/missing, coverage, and full integrity values. Markdown output is compact; inspect JSON for SSE and all detailed fields.

Visibility is measured at the first public observer and reconciled via persisted trade→pool-event IDs. It does not prove private per-frame delivery. SSE bytes retain the previous metric's semantics: JSON data payload bytes, excluding SSE framing and HTTP overhead. Queues/resources use one-second samples and are not exact instantaneous maxima. Success TPS counts responses completed during the capacity window; successful drain responses remain separate.

## Verification performed

- `/tmp/sunyunbo/thccb-pr23-review-20261023/review-venv/bin/python -m py_compile` on driver.py, sampler.py, summarize.py: passed.
- Same interpreter `driver.py --help` and `summarize.py --help`: passed.
- Manual review of user-count propagation, all old fixed user/stream/total-cash constants, process/private cleanup, current-only sampler path, pool separation, and original public/private connection admission: completed.
- No product tests or load run executed. Runtime connection and integrity still need the approved short baseline pilot.
