#!/usr/bin/env bash
# 看门狗：崩了 30s 后重拉；exit 0（kill switch / FatalAuth）不重拉
set -u
cd "$(dirname "$0")"
source .venv/bin/activate

while true; do
  echo "[$(date '+%F %T')] starting trader"
  python -m thccb_quant
  EXIT=$?
  if [ $EXIT -eq 0 ]; then
    echo "[$(date '+%F %T')] clean exit, watchdog quitting"
    break
  fi
  echo "[$(date '+%F %T')] crashed exit=$EXIT, restart in 30s"
  sleep 30
done
