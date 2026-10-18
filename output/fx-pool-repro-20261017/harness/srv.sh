#!/bin/bash
set -euo pipefail
BASE=/tmp/sunyunbo/fx-pool-repro-20261017
ACT=${1:?start|stop}; NAME=${2:-}
export PYTHONPATH=/tmp/sunyunbo/thccb-fx-loadtest-20261017/backend
export DATABASE_URL='postgresql+asyncpg://sunyunbo@127.0.0.1:55570/thccb_lt_pool_test'
export SECRET_KEY='repro-secret-key-0123456789abcdef0123456789abcdef'
export APP_ENV=development NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1
if [ "$ACT" = stop ]; then
  [ -f "$BASE/server.pid" ] && kill "$(cat "$BASE/server.pid")" 2>/dev/null || true
  rm -f "$BASE/server.pid"; sleep 1; echo stopped
  exit 0
fi
export REPRO_DIR="$BASE/$NAME"
rm -rf "$REPRO_DIR"; mkdir -p "$REPRO_DIR"
nohup /data/sunyunbo/www/TouhouCCB/backend/venv/bin/python "$BASE/serve2.py" >"$REPRO_DIR/server.log" 2>&1 &
echo $! > "$BASE/server.pid"
for _ in $(seq 1 60); do
  if curl -sf -o /dev/null http://127.0.0.1:55571/api/v1/fx/pairs; then break; fi
  sleep 0.25
done
echo "started pid=$(cat "$BASE/server.pid") repro=$REPRO_DIR"
