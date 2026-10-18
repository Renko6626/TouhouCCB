# Task6 preparation — no services started

- Fixed before: `a820157e2555e2143d37864962accf74a8f006a0`, extracted by `git archive`; includes financing `b915afb` and merged dependency query `71b8a83`.
- After: **not extracted**. Wait for main-agent final commit, then `backend/venv/bin/python /tmp/sunyunbo/fx-market-memory-ab-20261018/prepare.py --after-commit <exact-final-commit>`; do not snapshot a moving worktree.
- Source manifest: root output `before-source.json`; after-source.json and source.diff will be generated only from final commit.
- Before/after share one lightweight `count_server.py` and load driver, pool10+20, one Uvicorn worker18543, isolated PG14 Unix socket55489, new `fx_market_memory_{before,after}_test` databases. No profiler.

## Read-heavy prelude

After the original 100-user/200-SSE admission and 60/20/20 role preparation, insert 60s `readheavy` before the original readonly/normal/active/stress phases.

- 100 users, each 3 rounds at 20s spacing; per-user start stagger0.15s. Each round issues context, versions, prices(current fx key), snapshot exactly once: planned1200 fixed HTTP reads. Concurrent fixed-read cap10.
- Planned30 real quote+trade cycles, scheduled every2s across the first30 users (18cash/6finance/6short). No retries. A slow cycle can delay its subsequent scheduled cycle; actual starts/completions are reported.
- Fixed-count planning does not bypass protection: the original 30s rolling >5% error / successful-trade P99>2s protection also applies to readheavy. Abort means fixed reads may be incomplete and must be reported.
- Then retain the original readonly30s, normal60s, active45s, stress30s and burst-observe10s; stress is aggregate25cycles/s. Mixed phases begin after the readheavy prelude, and therefore after its real account writes; both variants follow the same sequence.
- Original periodic readers are not added to readheavy. Its fixed reads, trade-cycle page refreshes, SSE traffic and history repairs are reported separately.

## Evidence

SQLAlchemy cursor execution counts by phase/route/category: market row inputs, public share-lock identity/version, account, dependencies, history, market metadata and volume/history queries remain distinct. Shared version guard is not called a full market load. SQL rows also carry route-family attribution: business_quote_or_write (actual FX trades/risk/net/short/borrow quote DB reads remain intentional), live_market_read (context/versions/prices/streams/pure market HTTP), synthetic publisher component labels, history and untagged background. The /fx/pairs discovery/metadata directory and personal my-trades joins to Pair remain DB-backed and have separate retained families. Never target all Pair SELECTs as zero; evaluate warmed current-market inputs only, and report retained pool_version FOR SHARE separately. Broad SQL categories are inspectable and must be checked against final query shape before interpreting warm cache misses. Cursor counters exclude driver pre_ping/control SQL.

Same light resources/pool/gates/eventloop hooks for both. Independent0.5s PG activity/queryid/blocker/backend-CPU monitor; phase-entry and stress-stop pg_stat_statements/WAL snapshots. SSE body counts/bytes, client freshness/receive interval and dynamic chart/history request composition are preserved. Synthetic route labels are not async causality. Stop epoch separates stress from drain/cancellation; no natural-recovery claim from cancelled streams.

Readonly after prelude observes a no-trade period, but a30s window cannot by itself prove a24h volume expiration. Do not alter committed rows behind the memory cache to manufacture expiration; rely on the focused correctness scenario for clock-boundary behavior unless the main agent requests a separate isolated fixture.

## Deferred commands

`run.py` refuses to start while after/backend is absent. The main agent has now started the isolated PG for implementation checks and owns its lifecycle. Do not start/stop it or run load until final-code notification and coordination with correctness tests; HTTP18543 remains stopped.

PG init/test command is in `pg-tests.sh`: uses the main-agent-created `fx_market_memory_test` only after explicit coordination (fixtures drop/create schema), uses a shell-quoted TEST_PG_DATABASE_URL, then explicitly runs the three brief PG files. Missing URL/skip is not success. Do not drop old test DBs.

Execution entry after authorization: `/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python /tmp/sunyunbo/fx-market-memory-ab-20261018/run.py`. Existing variant directories/new DB names are one-shot; do not overwrite prior results. Final run must clean services and tokens and produce report.md/analysis.json plus actual commands/hashes/raw logs.
