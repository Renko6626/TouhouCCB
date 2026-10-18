# Request-snapshot A/B preparation

Prepared only; no PostgreSQL connection, HTTP process or load started. Main agent owns the already-running PG14 socket55489. Correctness pytest may be running; do not start load or stop cluster until explicitly authorized.

- Repo: /tmp/sunyunbo/thccb-request-snapshot-20261018.
- Before: fixed330f4e683ec5564dea6ea59983351c3c8e128d9d via git archive, byte-for-byte hashes equal the previous market-memory experiment after snapshot.
- After: not extracted; wait for final commit and explicit load-start signal. prepare.py --after-commit EXACT_REF will extract a committed archive and save hashes/diff.
- New runtime /tmp/sunyunbo/fx-request-snapshot-ab-20261018; output root/output/fx-request-snapshot-ab-20261018; new databases fx_request_snapshot_{before,after}_test. Old databases untouched.
- Same previous workload:100 users, public/private SSE100 each, cash/finance/short60/20/20; readheavy60s with1200 fixed reads and30 scheduled real quote/trade cycles, readonly30s, normal60s, active45s, stress30s aggregate25cycles/s, existing burst10s. Same seed20261017, pool10+20, single Uvicorn18543. Original30s error>5%/successful-tradeP99>2s protection retained. No profiler or added workload.
- Same light SQL/GATE/pool/eventloop/PG monitoring and corrected lifespan_original naming. Account/context read tails, periodic versions, prices, target-write held/wait and SQL/pool/throughput must be reported independently. SQL directory/my-trades/business/pool_version/history distinctions remain. Actual HTTP composition, SSE freshness/frames, protection/drain boundaries and any regressions stay visible.
- run.py refuses execution until after/backend exists. Do not execute it merely because an after archive exists: final explicit main-agent start signal is also required.
- prior_report_template.py is the prior round layout and contains its measured conclusions; it is intentionally not an executable new report entrypoint. Final reporting will derive new conclusions only after the new A/B raw data exists.
- At run end stop only this harness HTTP/monitor processes and remove temporary tokens; leave PG lifecycle to main agent. No production edit, git action or deployment.

## Interpretation boundary

The100-user workload targets one FX pool. When collateral dependency equals the target, prior shared intent was already merged into target exclusive. This A/B mainly measures removal of ordinary-read queueing; do not claim throughput gains from removing cross-LMSR/other-FX collateral locks. Cross-market correctness is covered by implementer-controlled concurrency tests; cross-market performance remains unmeasured. No additional workload or writer-side work is authorized here.
