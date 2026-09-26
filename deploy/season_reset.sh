#!/usr/bin/env bash
# Host-side season maintenance. Run from the deployed project; never pulls images.
# preview is read-only. execute additionally requires the exact SEASON_CONFIRM=RESET.
set -euo pipefail
umask 077

ACTION="${1:-preview}"
case "$ACTION" in
    preview|backup|execute) ;;
    *) echo "Invalid action: use preview, backup or execute" >&2; exit 1 ;;
esac
if [ "$#" -gt 1 ]; then
    echo "Usage: bash deploy/season_reset.sh [preview|backup|execute]" >&2
    exit 1
fi
if [ "$ACTION" = execute ] && [ "${SEASON_CONFIRM:-}" != RESET ]; then
    echo "execute requires the exact SEASON_CONFIRM=RESET; no services changed" >&2
    exit 1
fi

cd "$(dirname "$0")/.."
PROJECT_ROOT="$(pwd)"
# Bound the remote process itself. Reserve two minutes to stop/wait maintenance
# containers and restore the backend after TERM; total is fourteen minutes.
if [ "${SEASON_RESET_WITH_DEADLINE:-}" != 1 ]; then
    export SEASON_RESET_WITH_DEADLINE=1
    exec timeout --signal=TERM --kill-after=120s 720 \
        bash "$PROJECT_ROOT/deploy/season_reset.sh" "$ACTION"
fi

RULESET=2026-09-27
HEALTH_URL=http://127.0.0.1:8004/api/v1/market/list
RESTORE_BACKEND=0
VERIFY_CREATED=0
VERIFY_DB=''
BACKUP_PATH=''
WORK_DIR=''
OVERRIDE_PATH=''
OWNER_TOKEN=''
ONEOFF_NAME=''
COMPOSE_PID=''

log() { printf '[season-reset] %s\n' "$*"; }

restore_backend() {
    if [ "$RESTORE_BACKEND" = 1 ]; then
        log "Restoring originally running backend"
        docker compose start backend || return $?
        RESTORE_BACKEND=0
    fi
}

finish_oneoff() {
    local container_id owned_id running
    # Kill/wait only our child client. It may otherwise forward TERM then keep
    # waiting for the container; the daemon container is handled independently.
    if [ -n "$COMPOSE_PID" ]; then
        kill -KILL "$COMPOSE_PID" 2>/dev/null || true
        wait "$COMPOSE_PID" 2>/dev/null || true
        COMPOSE_PID=''
    fi
    if [ -z "$ONEOFF_NAME" ]; then return 0; fi
    container_id="$(docker ps --all --quiet --filter "name=^/${ONEOFF_NAME}$")" || return $?
    if [ -n "$container_id" ]; then
        owned_id="$(docker ps --all --quiet --filter "id=$container_id" \
            --filter "label=thccb.season-reset.owner=${OWNER_TOKEN}")" || return $?
        if [ "$owned_id" != "$container_id" ]; then return 1; fi
        log "Stopping this run's maintenance container: $ONEOFF_NAME"
        if ! docker stop --time 30 "$container_id"; then
            # --rm may remove the container between discovery and stop. Query
            # the daemon successfully before treating an absent container as safe.
            running="$(docker ps --quiet --filter "id=$container_id")" || return $?
            if [ -n "$running" ]; then return 1; fi
        fi
        if ! docker wait "$container_id" > /dev/null; then
            running="$(docker ps --quiet --filter "id=$container_id")" || return $?
            if [ -n "$running" ]; then return 1; fi
        fi
    fi
    ONEOFF_NAME=''
}

drop_verification_database() {
    if [ "$VERIFY_CREATED" = 1 ]; then
        # VERIFY_CREATED is set only after this invocation's createdb succeeded.
        docker compose exec -T postgres dropdb -U thccb "$VERIFY_DB" || return $?
        VERIFY_CREATED=0
    fi
}

cleanup() {
    local result=$?
    local safe_to_restart=1
    trap - EXIT
    trap '' INT TERM
    set +e
    if ! finish_oneoff; then
        log "ERROR: maintenance container exit is unverified; backend kept stopped, manual recovery required" >&2
        result=1
        safe_to_restart=0
    fi
    if [ "$VERIFY_CREATED" = 1 ]; then
        if ! drop_verification_database; then
            log "ERROR: cannot remove validation database $VERIFY_DB; manual cleanup required" >&2
            result=1
        fi
    fi
    if [ "$RESTORE_BACKEND" = 1 ] && [ "$safe_to_restart" = 1 ]; then
        if ! restore_backend; then
            log "ERROR: cannot restore backend; manual recovery required" >&2
            result=1
        fi
    fi
    if [ -n "$WORK_DIR" ]; then
        rm -f -- "$OVERRIDE_PATH" "$WORK_DIR/confirmation"
        if ! rmdir -- "$WORK_DIR"; then
            log "ERROR: cannot remove temporary maintenance configuration: $WORK_DIR" >&2
            result=1
        fi
    fi
    if [ "$result" != 0 ]; then
        log "FAILED: no automatic production database restore was attempted" >&2
        if [ -n "$BACKUP_PATH" ]; then log "Backup artifacts: $BACKUP_PATH" >&2; fi
    fi
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

WORK_DIR="$(mktemp -d "$PROJECT_ROOT/.season-reset.XXXXXX")"
OVERRIDE_PATH="$WORK_DIR/compose.yml"
OWNER_TOKEN="${WORK_DIR##*/}-$$"
printf '%s\n' '{"services":{"backend":{"init":true,"pull_policy":"never"}}}' > "$OVERRIDE_PATH"
printf 'RESET\n' > "$WORK_DIR/confirmation"

run_oneoff() {
    local stage=$1 input_path=$2 result=0
    shift 2
    ONEOFF_NAME="thccb-season-${OWNER_TOKEN}-${stage}"
    docker compose -f "$PROJECT_ROOT/docker-compose.yml" -f "$OVERRIDE_PATH" \
        run --rm --no-deps -T --pull never --name "$ONEOFF_NAME" \
        --label "thccb.season-reset.owner=$OWNER_TOKEN" backend python "$@" < "$input_path" &
    COMPOSE_PID=$!
    # Bash's builtin wait is interrupted immediately by TERM, so cleanup does
    # not wait for a foreground Compose client before stopping its container.
    wait "$COMPOSE_PID" || result=$?
    COMPOSE_PID=''
    if [ "$result" = 0 ]; then ONEOFF_NAME=''; fi
    return "$result"
}

preview() {
    run_oneoff preview /dev/null scripts/season_reset.py --dry-run --expected-ruleset "$RULESET"
}

health_check() {
    local attempt
    for ((attempt=1; attempt<=12; attempt++)); do
        if curl -sf --max-time 5 "$HEALTH_URL" > /dev/null; then
            log "Backend health check passed"
            return 0
        fi
        if [ "$attempt" -lt 12 ]; then sleep 2; fi
    done
    log "ERROR: backend health check failed" >&2
    return 1
}

# Run the deployed ruleset before any backup or service changes. Unsupported
# --expected-ruleset makes an old backend image fail safely at this boundary.
preview
if [ "$ACTION" = preview ]; then
    log "Preview complete; no backup, stop or reset was performed"
    exit 0
fi

BACKEND_RUNNING="$(docker compose ps --status running -q backend)"
if [ -n "$BACKEND_RUNNING" ]; then
    # Set before stop, so a failed/partial stop still triggers service recovery.
    RESTORE_BACKEND=1
    log "Stopping backend and draining its writers before backup"
    docker compose stop backend
fi

STAMP="$(date -u '+%Y%m%d_%H%M%S')_$$"
VERIFY_DB="thccb_season_verify_${STAMP}"
BACKUP_PATH="$PROJECT_ROOT/backups/thccb_pre_season_${STAMP}.dump"
mkdir -p "$PROJECT_ROOT/backups"
# No invocation may overwrite a prior backup or verification artifact.
set -o noclobber
log "Creating fresh backup: $BACKUP_PATH"
docker compose exec -T postgres pg_dump -Fc -U thccb thccb > "$BACKUP_PATH"
test -s "$BACKUP_PATH"
sha256sum "$BACKUP_PATH" > "$BACKUP_PATH.sha256"
sha256sum --check "$BACKUP_PATH.sha256"
docker compose exec -T postgres pg_restore --list < "$BACKUP_PATH" > "$BACKUP_PATH.toc"
test -s "$BACKUP_PATH.toc"

log "Verifying full restore in new database: $VERIFY_DB"
docker compose exec -T postgres createdb -U thccb --template=template0 "$VERIFY_DB"
VERIFY_CREATED=1
docker compose exec -T postgres pg_restore --exit-on-error -U thccb -d "$VERIFY_DB" < "$BACKUP_PATH"
drop_verification_database
printf 'verified_full_restore=%s\nruleset=%s\n' "$VERIFY_DB" "$RULESET" > "$BACKUP_PATH.verified"
log "Backup verified by full restore: $BACKUP_PATH"

if [ "$ACTION" = execute ]; then
    log "Executing season reset using the verified backup"
    run_oneoff reset "$WORK_DIR/confirmation" scripts/season_reset.py --expected-ruleset "$RULESET"
fi

restore_backend
if [ -n "$BACKEND_RUNNING" ]; then
    health_check
else
    log "Backend was stopped before maintenance and remains stopped"
fi

if [ "$ACTION" = execute ]; then
    run_oneoff audit /dev/null scripts/audit_verify.py
    log "Season reset complete; audit verification passed"
else
    log "Backup complete; no season reset was performed"
fi
