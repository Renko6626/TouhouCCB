#!/usr/bin/env bash
# Run only via the dedicated workflow. Stage files are never live before backup.
set -euo pipefail
umask 077
ROOT=/home/deploy/TouhouCCB
STAGE=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
[[ "${REBUILD_RUN_ID:-}" =~ ^[0-9]{1,20}$ ]] || { echo 'Invalid run ID' >&2; exit 1; }
[[ "$STAGE" = "$ROOT/.fx-rebuild-$REBUILD_RUN_ID" ]] || { echo 'Unexpected staging directory' >&2; exit 1; }
[[ "${EXPECTED_BUILD_SHA:-}" =~ ^[0-9a-f]{40}$ ]] || exit 1
[[ "${BACKEND_IMAGE:-}" = *":$EXPECTED_BUILD_SHA" ]] || exit 1
[[ "${REBUILD_ACTION:-}" = preview || "${REBUILD_ACTION:-}" = execute ]] || exit 1
[ -f .env ] || exit 1
mkdir -p backups
exec 9>backups/.fx-rebuild.lock
flock -n 9 || { echo 'Another maintenance operation is running' >&2; exit 1; }
BACKUP="$ROOT/backups/fx-rebuild-$REBUILD_RUN_ID"
# No reuse: exported files and databases belong to one attempt.
mkdir -m 700 "$BACKUP"
TARGET="thccb_rebuild_$REBUILD_RUN_ID"
OLD="thccb_before_$REBUILD_RUN_ID"
VERIFY="thccb_verify_$REBUILD_RUN_ID"
OVERRIDE="$BACKUP/compose.override.yml"
ONEOFF="thccb-fx-rebuild-$REBUILD_RUN_ID"
STOPPED=0
CUTOVER=0
VERIFY_CREATED=0
log() { echo "[$(date -u +%H:%M:%S)] $*"; }
fail() { log "FAILED: $*" >&2; exit 1; }
# Keep compose project identity/volumes/network and use the live secret .env.
compose() { docker compose --project-directory "$ROOT" --env-file "$ROOT/.env" -f "$ROOT/docker-compose.yml" "$@"; }
newcompose() { docker compose --project-directory "$ROOT" --env-file "$ROOT/.env" -f "$STAGE/docker-compose.yml" -f "$OVERRIDE" "$@"; }
pg() { compose exec -T postgres "$@"; }
cleanup() {
    code=$?
    trap - EXIT
    docker rm -f "$ONEOFF" >/dev/null 2>&1 || true
    if [ "$VERIFY_CREATED" = 1 ]; then pg dropdb -U thccb --if-exists "$VERIFY" >/dev/null 2>&1 || true; fi
    if [ "$code" != 0 ]; then
        if [ "$CUTOVER" = 1 ]; then
            newcompose stop backend >/dev/null 2>&1 || true
            log "Cutover attempted: backend stays stopped. Retain databases $OLD/$TARGET/thccb and $BACKUP; explicit recovery required."
        elif [ "$STOPPED" = 1 ]; then
            if compose start backend >/dev/null 2>&1; then
                log 'Before cutover: previous backend container restarted.'
            else
                log 'Before cutover: previous backend restart failed; backend requires explicit operator recovery.' >&2
            fi
        fi
    fi
    exit "$code"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
cat > "$OVERRIDE" <<EOF
services:
  backend:
    image: "$BACKEND_IMAGE"
    environment:
      THCCB_READ_ONLY_INSTANCE: "true"
EOF
run_tool() {
    # The private bind-mounted directory is owned by this deployment account.
    # Compose's application UID may differ; do not loosen backup permissions.
    newcompose run --rm --no-deps -T --user "$(id -u):$(id -g)" --name "$ONEOFF" backend python scripts/production_fx_rebuild.py "$1" \
        --run-id "$REBUILD_RUN_ID" --balance "${REBUILD_BALANCE:-0}" \
        --directory "/app/backups/fx-rebuild-$REBUILD_RUN_ID" "${@:2}"
}
health() {
    for attempt in $(seq 1 30); do
        if curl -sf --max-time 10 http://127.0.0.1:8004/api/v1/market/list >/dev/null; then
            actual=$(docker inspect --format='{{range .Config.Env}}{{println .}}{{end}}' thccb-backend | sed -n 's/^APP_BUILD_SHA=//p')
            [ "$actual" = "$EXPECTED_BUILD_SHA" ] || fail 'Running build SHA differs'
            return
        fi
        sleep 3
    done
    fail 'Backend health failed'
}
# Refuse another application container on this compose network. Stopping the
# named backend is insufficient if an operator left a second writer running.
NETWORK=$(docker inspect --format='{{range $name, $value := .NetworkSettings.Networks}}{{println $name}}{{end}}' thccb-postgres)
[ "$(echo "$NETWORK" | wc -l)" = 1 ] || fail 'Unexpected postgres networks'
for container in $(docker network inspect "$NETWORK" --format='{{range .Containers}}{{println .Name}}{{end}}'); do
    case "$container" in thccb-postgres|thccb-backend) ;; *) fail 'Unexpected network container; stop all other writers first';; esac
done
newcompose pull backend
run_tool config
if [ "$REBUILD_ACTION" = preview ]; then
    run_tool preview
    log "Preview only; source unchanged. Private manifest: $BACKUP"
    rm "$OVERRIDE"
    exit 0
fi
[ "${REBUILD_CONFIRMATION:-}" = REBUILD ] || fail 'Confirmation must be REBUILD'
[ -n "${REBUILD_BALANCE:-}" ] || fail 'Explicit balance is required'
[ -d "$STAGE/dist" ] && [ -s "$STAGE/dist/index.html" ] || fail 'Frontend artifact missing'
[ -d thccb-frontend/dist ] || fail 'Live frontend missing'
[ "$(pg psql -U thccb -d postgres -tAc "SELECT count(*) FROM pg_database WHERE datname IN ('$OLD','$TARGET','$VERIFY');" | tr -d '[:space:]')" = 0 ] || fail 'Run-specific database already exists'
docker inspect --format='{{.Id}} {{.Image}}' thccb-backend > "$BACKUP/old-backend.txt"
cp docker-compose.yml "$BACKUP/docker-compose.yml"
cp -a deploy "$BACKUP/deploy"
cp -a thccb-frontend/dist "$BACKUP/frontend-dist"
# Mark before stopping so interrupted stop is also recovered pre-cutover.
STOPPED=1
compose stop backend
# This includes unexpected external sessions. Do not kill unknown writers.
[ "$(pg psql -U thccb -d postgres -tAc "SELECT count(*) FROM pg_stat_activity WHERE datname='thccb';" | tr -d '[:space:]')" = 0 ] || fail 'Source still has database sessions'
pg pg_dump -Fc -U thccb thccb > "$BACKUP/source.dump"
[ -s "$BACKUP/source.dump" ] || fail 'Empty backup'
sha256sum "$BACKUP/source.dump" > "$BACKUP/source.dump.sha256"
sha256sum -c "$BACKUP/source.dump.sha256" >/dev/null
pg createdb -U thccb --template=template0 "$VERIFY"
VERIFY_CREATED=1
pg pg_restore --exit-on-error -U thccb -d "$VERIFY" < "$BACKUP/source.dump" > "$BACKUP/restore.log" 2>&1 || fail 'Backup restore failed; inspect private restore.log'
pg dropdb -U thccb "$VERIFY"
VERIFY_CREATED=0
log "Full backup restore verified: $BACKUP/source.dump"
run_tool export
pg createdb -U thccb --template=template0 "$TARGET"
run_tool import
run_tool verify
[ "$(pg psql -U thccb -d postgres -tAc "SELECT count(*) FROM pg_stat_activity WHERE datname IN ('thccb','$TARGET');" | tr -d '[:space:]')" = 0 ] || fail 'Active sessions prevent cutover'
# Block new source/target connections, then recheck. Failed first rename leaves
# the old source intact but stopped, requiring an explicit operator recovery.
CUTOVER=1
pg psql -v ON_ERROR_STOP=1 -U thccb -d postgres -c "ALTER DATABASE thccb ALLOW_CONNECTIONS false; ALTER DATABASE \"$TARGET\" ALLOW_CONNECTIONS false;"
[ "$(pg psql -U thccb -d postgres -tAc "SELECT count(*) FROM pg_stat_activity WHERE datname IN ('thccb','$TARGET');" | tr -d '[:space:]')" = 0 ] || fail 'Sessions appeared during cutover'
pg psql -v ON_ERROR_STOP=1 -U thccb -d postgres -c "ALTER DATABASE thccb RENAME TO \"$OLD\";"
pg psql -v ON_ERROR_STOP=1 -U thccb -d postgres -c "ALTER DATABASE \"$TARGET\" RENAME TO thccb;"
# Old database stays inaccessible except for explicit verification below.
pg psql -v ON_ERROR_STOP=1 -U thccb -d postgres -c 'ALTER DATABASE thccb ALLOW_CONNECTIONS true;'
cp "$STAGE/docker-compose.yml" docker-compose.yml
cp -a "$STAGE/deploy/." deploy/
mv thccb-frontend/dist "$BACKUP/frontend-live-at-cutover"
mv "$STAGE/dist" thccb-frontend/dist
newcompose up -d --no-deps backend
health
newcompose run --rm --no-deps -T --name "$ONEOFF" backend python scripts/audit_verify.py > "$BACKUP/audit-verify.log" 2>&1 || fail 'Audit verification failed; inspect private audit-verify.log'
log 'Audit replay verified'
run_tool verify --after-cutover
newcompose stop backend
# Replace explicit read-only value; container env wins over mounted .env.
sed -i 's/THCCB_READ_ONLY_INSTANCE: "true"/THCCB_READ_ONLY_INSTANCE: "false"/' "$OVERRIDE"
newcompose up -d --no-deps backend
health
# Only now delete the old physical DB. Dump and frontend remain recoverable.
pg dropdb -U thccb "$OLD"
rm "$OVERRIDE"
log "Rebuild complete: backup=$BACKUP balance=$REBUILD_BALANCE version=$EXPECTED_BUILD_SHA; economic gates remain closed."
