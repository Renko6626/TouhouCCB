#!/usr/bin/env bash
# Run manually on the server after deploying the matching backend image.
# Does not alter nginx, pull images, or run HTTP load on the server.
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."

ACTION="${1:-help}"
TAG="${2:-}"
case "$ACTION" in
  prepare|mint|inspect|cleanup) ;;
  *) echo 'Usage: bash deploy/fx_loadtest.sh {prepare|mint|inspect|cleanup} RUN_ID [SOURCE_PAIR_ID]'; exit 1 ;;
esac
[[ "$TAG" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,39}$ ]] || { echo 'Invalid run ID' >&2; exit 1; }
if [ "$ACTION" = prepare ]; then
  [[ "${3:-}" =~ ^[1-9][0-9]*$ ]] || { echo 'prepare requires SOURCE_PAIR_ID' >&2; exit 1; }
  [ "$#" -eq 3 ] || { echo 'Unexpected arguments' >&2; exit 1; }
else
  [ "$#" -eq 2 ] || { echo 'Unexpected arguments' >&2; exit 1; }
fi

# Fail before stopping anything if the deployed image lacks this tool.
docker compose run --rm --no-deps --pull never -T backend python -m scripts.fx_loadtest --help > /dev/null

run_cli() {
  docker compose run --rm --no-deps --pull never -T backend python -m scripts.fx_loadtest "$@" --run-id "$TAG"
}

if [ "$ACTION" = mint ] || [ "$ACTION" = inspect ]; then
  run_cli "$ACTION"
  if [ "$ACTION" = mint ]; then
    echo "Private token file on host: backups/fx-loadtest/$TAG/tokens.json"
    echo 'Copy it to the load generator privately; do not paste it into chat or commit it.'
  fi
  exit
fi

if [ "$ACTION" = cleanup ]; then
  run_cli cleanup
  read -r -p "Delete only run $TAG and restore its original market? Type CLEANUP: " answer
  [ "$answer" = CLEANUP ] || { echo 'Cancelled'; exit 0; }
else
  echo "Will pause the active source market $3 and create a dedicated market plus 100 ordinary test users."
  read -r -p 'Confirm maintenance window; type PREPARE: ' answer
  [ "$answer" = PREPARE ] || { echo 'Cancelled'; exit 0; }
fi

# Existing Docker deployment has one backend containing all economic jobs.
# External writers, if any, must be stopped separately by the operator.
echo 'Ensure no external economic writers are running. Backing up before maintenance.'
BACKUP="backups/fx-loadtest/${TAG}-${ACTION}-$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
mkdir -p backups/fx-loadtest
chmod 700 backups/fx-loadtest
WAS_RUNNING=0
if [ -n "$(docker compose ps --status running -q backend)" ]; then
  WAS_RUNNING=1
fi
MAINT_PID=''
MAINT_NAME="thccb-fx-lt-${ACTION}-$$"
finish() {
  local result=$?
  trap - EXIT INT TERM
  if [ -n "$MAINT_PID" ]; then
    # A cancelled Compose client does not prove the DB process stopped.
    # Leave the backend stopped unless this run's container is verified gone.
    docker stop --time 15 "$MAINT_NAME" > /dev/null 2>&1 || true
    wait "$MAINT_PID" 2>/dev/null || true
    local running
    if ! running=$(docker ps -q --filter "name=^/${MAINT_NAME}$"); then
      echo 'Cannot verify maintenance exit; backend remains stopped.' >&2
      exit 1
    fi
    if [ -n "$running" ]; then
      echo 'Maintenance container still running; backend remains stopped. Inspect before restarting.' >&2
      exit 1
    fi
  fi
  if [ "$WAS_RUNNING" = 1 ]; then
    docker compose start backend || result=1
    echo 'Backend start requested; verify JSON from /api/v1/fx/pairs and check docker compose logs backend.'
  fi
  exit "$result"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

docker compose stop backend
docker compose exec -T postgres pg_dump -U thccb -d thccb | gzip > "$BACKUP"
gzip -t "$BACKUP"
echo "Backup: $BACKUP"
ARGS=("$ACTION" --run-id "$TAG" --confirm-stopped)
if [ "$ACTION" = prepare ]; then
  ARGS+=(--source-pair "$3")
else
  ARGS+=(--execute)
fi
docker compose run --rm --no-deps --pull never -T --name "$MAINT_NAME" backend python -m scripts.fx_loadtest "${ARGS[@]}" &
MAINT_PID=$!
wait "$MAINT_PID"
MAINT_PID=''
echo "$ACTION completed. Backup remains on server; no full-database restore was performed."
