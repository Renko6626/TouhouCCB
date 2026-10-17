#!/usr/bin/env bash
set -euo pipefail
# Run from repository root. Fresh *_test DB names are required; never drop prior databases.
PY=/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python
TASK=/tmp/sunyunbo/fx-dependency-query-ab-20261018
"$PY" "$TASK/prepare.py"
"$PY" "$TASK/adapt.py"
/usr/lib/postgresql/14/bin/pg_ctl -D /tmp/sunyunbo/fx-pool-incident-20261017/pgdata -l "$TASK/postgres.log" -o "-k /tmp/sunyunbo/fx-pool-incident-20261017/socket -p 55489 -h '' -c shared_preload_libraries=pg_stat_statements -c compute_query_id=on -c track_io_timing=on -c track_wal_io_timing=on" start
trap '/usr/lib/postgresql/14/bin/pg_ctl -D /tmp/sunyunbo/fx-pool-incident-20261017/pgdata -m fast stop' EXIT
"$PY" "$TASK/run.py"
"$PY" "$TASK/build_report.py"
