#!/bin/bash
set -euo pipefail
BASE=/tmp/sunyunbo/fx-pool-repro-20261017
export PYTHONPATH=/tmp/sunyunbo/thccb-fx-loadtest-20261017/backend
export DATABASE_URL='postgresql+asyncpg://sunyunbo@127.0.0.1:55570/thccb_lt_pool_test'
export SECRET_KEY='repro-secret-key-0123456789abcdef0123456789abcdef'
export APP_ENV=development NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1
export REPRO_DIR="$BASE/run7"
rm -rf "$REPRO_DIR"; mkdir -p "$REPRO_DIR"
for _ in 1 2; do
  taskset -c 5 bash -c 'while :; do :; done' &
  echo $! >> "$BASE/hog.pids"
done
taskset -c 5 nohup /data/sunyunbo/www/TouhouCCB/backend/venv/bin/python "$BASE/serve2.py" >"$REPRO_DIR/server.log" 2>&1 &
echo $! > "$BASE/server.pid"
for _ in $(seq 1 120); do
  if curl -sf -o /dev/null http://127.0.0.1:55571/api/v1/fx/pairs; then break; fi
  sleep 0.25
done
echo "throttled server pid=$(cat "$BASE/server.pid") hogs=$(tr '\n' ' ' < "$BASE/hog.pids")"
