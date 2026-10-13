#!/usr/bin/env bash
# Run only via the dedicated workflow. Stage files are never live before backup.
set -euo pipefail
umask 077
ROOT=/home/deploy/TouhouCCB
STAGE=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
[[ "${RESET_RUN_ID:-}" =~ ^[0-9]{1,20}$ ]] || { echo 'Invalid run ID' >&2; exit 1; }
[[ "$STAGE" = "$ROOT/.business-reset-$RESET_RUN_ID" ]] || { echo 'Unexpected staging directory' >&2; exit 1; }
[[ "${EXPECTED_BUILD_SHA:-}" =~ ^[0-9a-f]{40}$ ]] || exit 1
[[ "${BACKEND_IMAGE:-}" = *":$EXPECTED_BUILD_SHA" ]] || exit 1
[[ "${RESET_ACTION:-}" = preview || "${RESET_ACTION:-}" = execute ]] || exit 1
[ -f .env ] || exit 1
mkdir -p backups
exec 9>backups/.business-reset.lock
flock -n 9 || { echo 'Another maintenance operation is running' >&2; exit 1; }
BACKUP="$ROOT/backups/business-reset-$RESET_RUN_ID"
# No reuse: exported files and databases belong to one attempt.
mkdir -m 700 "$BACKUP"
TARGET="thccb_reset_$RESET_RUN_ID"
OLD="thccb_before_$RESET_RUN_ID"
VERIFY="thccb_verify_$RESET_RUN_ID"
OVERRIDE="$BACKUP/compose.override.yml"
ONEOFF="thccb-business-reset-$RESET_RUN_ID"
OWNER_LABEL="thccb.business-reset.owner=$RESET_RUN_ID"
STOPPED=0
CUTOVER=0
CONNECTIONS_BLOCKED=0
OWNER_STARTED=0
ORIGIN_RUNNING=0
ROLE_SQL=
READONLY_RESTORE=
VERIFY_CREATED=0
stage() { printf "%s\n" "$1" > "$BACKUP/stage"; }
log() { echo "[$(date -u +%H:%M:%S)] $*"; }
fail() { log "FAILED: $*" >&2; exit 1; }
# Keep compose project identity/volumes/network and use the live secret .env.
compose() { docker compose --project-directory "$ROOT" --env-file "$ROOT/.env" -f "$ROOT/docker-compose.yml" "$@"; }
newcompose() { docker compose --project-directory "$ROOT" --env-file "$ROOT/.env" -f "$STAGE/docker-compose.yml" -f "$OVERRIDE" "$@"; }
pg() { compose exec -T postgres "$@"; }
cleanup() {
    code=$?
    trap - EXIT
    # A client exit must not leave an importer alive when the old owner restarts.
    if ! owned=$(docker ps --all --quiet --filter "name=^/$ONEOFF$"); then
        log "Cannot inspect maintenance container; explicit recovery required." >&2
        exit 1
    fi
    if [ -n "$owned" ]; then
        labelled=$(docker ps --all --quiet --filter "name=^/$ONEOFF$" --filter "label=$OWNER_LABEL") || { log 'Cannot verify maintenance ownership; explicit recovery required.' >&2; exit 1; }
        [ "$labelled" = "$owned" ] || { log 'Foreign maintenance container; backend remains stopped for explicit recovery.' >&2; exit 1; }
    fi
    if [ -n "$owned" ] && ! docker rm -f "$ONEOFF" >/dev/null 2>&1; then
        log 'Cannot confirm maintenance container removal; explicit recovery required.' >&2
        exit 1
    fi
    if [ "$VERIFY_CREATED" = 1 ]; then pg dropdb -U thccb --if-exists "$VERIFY" >/dev/null 2>&1 || true; fi
    if [ "$code" != 0 ]; then
        if [ "$OWNER_STARTED" = 1 ]; then
            if ! newcompose stop backend >/dev/null 2>&1; then
                log 'Failed to stop the new owner; explicit operator stop required.' >&2
            fi
            log "Owner start was attempted: new backend stopped if possible; no automatic rollback. Preserve both databases, new data and $BACKUP; inspect stage before recovery."
        elif [ "$CUTOVER" = 1 ]; then
            newcompose stop backend >/dev/null 2>&1 || true
            log "Cutover attempted: backend stays stopped. Retain databases $OLD/$TARGET/thccb and $BACKUP; explicit recovery required."
        elif [ "$STOPPED" = 1 ]; then
            if [ "$CONNECTIONS_BLOCKED" = 1 ]; then
                pg psql -v ON_ERROR_STOP=1 -U thccb -d postgres -c 'ALTER DATABASE thccb ALLOW_CONNECTIONS true;' >/dev/null 2>&1 || { log 'Source connection recovery failed; backend stays stopped.' >&2; exit 1; }
            fi
            [ "$ORIGIN_RUNNING" = 1 ] || exit "$code"
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
    local expected=()
    [ -z "${EXPECTED_INITIAL_BALANCE:-}" ] || expected=(--expected-initial-balance "$EXPECTED_INITIAL_BALANCE")
    newcompose run --rm --no-deps -T --user "$(id -u):$(id -g)" --name "$ONEOFF" --label "$OWNER_LABEL" backend python scripts/business_reset.py "$1" \
        --run-id "$RESET_RUN_ID" "${expected[@]}" \
        --directory "/app/backups/business-reset-$RESET_RUN_ID" "${@:2}"
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
if [ "$RESET_ACTION" = preview ]; then
    run_tool preview
    log "Preview only; source unchanged. Private run directory: $BACKUP"
    rm "$OVERRIDE"
    exit 0
fi
[ "${DATABASE_RESET_PENDING:-}" = true ] || fail "DATABASE_RESET_PENDING must be true"
[ "${RESET_CONFIRMATION:-}" = RESET ] || fail 'Confirmation must be RESET'
[ -n "${EXPECTED_INITIAL_BALANCE:-}" ] || fail 'Expected source initial_balance is required'
[ -d "$STAGE/dist" ] && [ -s "$STAGE/dist/index.html" ] || fail 'Frontend artifact missing'
[ -d thccb-frontend/dist ] || fail 'Live frontend missing'
[ "$(pg psql -U thccb -d postgres -tAc "SELECT count(*) FROM pg_database WHERE datname IN ('$OLD','$TARGET','$VERIFY');" | tr -d '[:space:]')" = 0 ] || fail 'Run-specific database already exists'
docker inspect --format='{{.Id}} {{.Image}}' thccb-backend > "$BACKUP/old-backend.txt"
old_image=$(docker inspect --format='{{.Image}}' thccb-backend)
docker image save "$old_image" > "$BACKUP/old-backend.image.tar"
[ -s "$BACKUP/old-backend.image.tar" ] || fail 'Old backend image backup missing'
cp docker-compose.yml "$BACKUP/docker-compose.yml"
cp -a deploy "$BACKUP/deploy"
cp -a thccb-frontend/dist "$BACKUP/frontend-dist"
# Mark before stopping so interrupted stop is also recovered pre-cutover.
[ -z "$(compose ps --status running -q backend)" ] || ORIGIN_RUNNING=1
[ "$(sed -n 's/^proxy_cache_path \([^ ]*\).*/\1/p' "$STAGE/deploy/nginx.conf")" = /var/cache/nginx/thccb ] || fail 'Unexpected history cache path'
sudo -n test -d /var/cache/nginx/thccb || fail 'History cache directory unavailable'
sudo -n find /var/cache/nginx/thccb -mindepth 1 -type f -name .business-reset-preflight-never-match -delete || fail 'History cache inaccessible'
stage stopping-writers
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
stage exporting
run_tool export
pg createdb -U thccb --template=template0 "$TARGET"
stage importing
run_tool import
run_tool verify
# Re-read the stopped source snapshot: CLI import refuses source/B drift, and
# this second preview refuses B changes before any connection/name changes.
run_tool preview
# Obtain only the SQL-quoted application role, never its password or URL.
ROLE_SQL=$(newcompose run --rm --no-deps -T --name "$ONEOFF" --label "$OWNER_LABEL" backend python -c 'from sqlalchemy.engine import make_url; from app.core.config import settings; u=make_url(settings.build_db_url()); assert u.username; print("\"" + u.username.replace("\"", "\"\"") + "\"")')
[[ "$ROLE_SQL" =~ ^\"[a-zA-Z_][a-zA-Z_0-9]*\"$ ]] || fail 'Invalid application role'
# Save exact previous role+database value as a restoration command. No global
# role setting and no other database is touched.
READONLY_RESTORE=$(pg psql -v ON_ERROR_STOP=1 -U thccb -d postgres -tAc "SELECT CASE WHEN v IS NULL THEN 'ALTER ROLE ' || quote_ident(r.rolname) || ' IN DATABASE thccb RESET default_transaction_read_only;' ELSE 'ALTER ROLE ' || quote_ident(r.rolname) || ' IN DATABASE thccb SET default_transaction_read_only TO ' || quote_literal(split_part(v,'=',2)) || ';' END FROM pg_roles r LEFT JOIN pg_db_role_setting s ON s.setrole=r.oid AND s.setdatabase=(SELECT oid FROM pg_database WHERE datname='$TARGET') LEFT JOIN LATERAL (SELECT value AS v FROM unnest(s.setconfig) value WHERE value LIKE 'default_transaction_read_only=%') cfg ON true WHERE r.rolname = '${ROLE_SQL:1:-1}';")
[ -n "$READONLY_RESTORE" ] || fail 'Application role not found'
printf '%s\n' "$READONLY_RESTORE" > "$BACKUP/readonly-restore.sql"
pg psql -v ON_ERROR_STOP=1 -U thccb -d postgres -c "ALTER ROLE $ROLE_SQL IN DATABASE \"$TARGET\" SET default_transaction_read_only TO on;"
stage target-readonly
[ "$(pg psql -U thccb -d postgres -tAc "SELECT count(*) FROM pg_stat_activity WHERE datname IN ('thccb','$TARGET');" | tr -d '[:space:]')" = 0 ] || fail 'Active sessions prevent cutover'
# Block new source/target connections, then recheck. Failed first rename leaves
# the old source intact but stopped, requiring an explicit operator recovery.
stage blocking-connections
CONNECTIONS_BLOCKED=1
pg psql -v ON_ERROR_STOP=1 -U thccb -d postgres -c "ALTER DATABASE thccb ALLOW_CONNECTIONS false; ALTER DATABASE \"$TARGET\" ALLOW_CONNECTIONS false;"
[ "$(pg psql -U thccb -d postgres -tAc "SELECT count(*) FROM pg_stat_activity WHERE datname IN ('thccb','$TARGET');" | tr -d '[:space:]')" = 0 ] || fail 'Sessions appeared during cutover'
stage first-rename-attempt
CUTOVER=1
pg psql -v ON_ERROR_STOP=1 -U thccb -d postgres -c "ALTER DATABASE thccb RENAME TO \"$OLD\";"
pg psql -v ON_ERROR_STOP=1 -U thccb -d postgres -c "ALTER DATABASE \"$TARGET\" RENAME TO thccb;"
# Old database stays inaccessible except for explicit verification below.
pg psql -v ON_ERROR_STOP=1 -U thccb -d postgres -c 'ALTER DATABASE thccb ALLOW_CONNECTIONS true;'
stage renamed
# Clear the exact server history cache configured in this deployment. Refuse
# unknown paths rather than deleting an operator-supplied directory.
sudo -n find /var/cache/nginx/thccb -mindepth 1 -type f -delete
cp "$STAGE/docker-compose.yml" docker-compose.yml
cp -a "$STAGE/deploy/." deploy/
mv thccb-frontend/dist "$BACKUP/frontend-live-at-cutover"
mv "$STAGE/dist" thccb-frontend/dist
stage readonly-acceptance
newcompose up -d --no-deps --force-recreate backend
health
newcompose run --rm --no-deps -T --name "$ONEOFF" --label "$OWNER_LABEL" backend python scripts/audit_verify.py > "$BACKUP/audit-verify.log" 2>&1 || fail 'Audit verification failed; inspect private audit-verify.log'
log 'Audit replay verified'
run_tool verify --after-cutover
newcompose stop backend
[ "$(pg psql -U thccb -d postgres -tAc "SELECT count(*) FROM pg_stat_activity WHERE datname='thccb';" | tr -d '[:space:]')" = 0 ] || fail 'Read-only sessions remain'
pg psql -v ON_ERROR_STOP=1 -U thccb -d postgres -c "$READONLY_RESTORE"
stage readonly-restored
# Replace explicit read-only value; container env wins over mounted .env.
sed -i 's/THCCB_READ_ONLY_INSTANCE: "true"/THCCB_READ_ONLY_INSTANCE: "false"/' "$OVERRIDE"
# Once owner start is attempted, never automatically roll back new accounts.
OWNER_STARTED=1
stage owner-start-attempt
newcompose up -d --no-deps --force-recreate backend
health
stage owner-healthy
# Only now delete the old physical DB. Dump and frontend remain recoverable.
pg dropdb -U thccb "$OLD"
stage complete
rm "$OVERRIDE"
log "Reset complete: backup=$BACKUP balance=$EXPECTED_INITIAL_BALANCE version=$EXPECTED_BUILD_SHA; economic gates remain closed."
