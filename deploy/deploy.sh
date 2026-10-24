#!/usr/bin/env bash
# TouhouCCB Docker 部署脚本
# 用法：bash deploy/deploy.sh
#
# CI 已完成：Docker 镜像构建推送到 GHCR，前端 dist/ 已 rsync 到位。
# 本脚本负责：拉取镜像 → 停经济写 → 验证备份 → 迁移 → 重启与健康检查。

set -euo pipefail
umask 077
cd "$(dirname "$0")/.."

PROJECT_ROOT="${THCCB_PROJECT_ROOT:-$(pwd)}"
if [ "${THCCB_COOLIFY:-}" = 1 ]; then
    # Invoked by Coolify on the host, with the generated Compose and .env.
    if docker ps --format '{{.Names}}' | grep -qx thccb-postgres; then
        echo 'Legacy PostgreSQL is still running. Stop it before mounting its data volume in Coolify.' >&2
        exit 1
    fi
    export COMPOSE_FILE="$PWD/docker-compose.coolify.yml"
    export COMPOSE_PROJECT_NAME="${PWD##*/}"
    [[ "$COMPOSE_PROJECT_NAME" =~ ^[a-z0-9]+$ ]] || exit 1
    BACKEND_IMAGE=$(docker compose config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["backend"]["image"])')
    EXPECTED_BUILD_SHA="${BACKEND_IMAGE##*:}"
    FRONTEND_IMAGE=$(docker compose config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["frontend"]["image"])')
    [ "${FRONTEND_IMAGE##*:}" = "$EXPECTED_BUILD_SHA" ] || { echo "Frontend/backend image SHA mismatch" >&2; exit 1; }
fi
BACKUP_DIR="$PROJECT_ROOT/backups"
HEALTH_URL="http://127.0.0.1:8004/health"
HEALTH_TIMEOUT=10
# 20×3s=60s 窗口：给冷启动期（schema check + seed_current_config + admin setup + APS）留余地
HEALTH_RETRIES=20
BACKEND_STOPPED=0
SCHEMA_ATTEMPTED=0
VERIFY_DB=''

# ── 工具函数 ──

log()  { echo "[$(date '+%H:%M:%S')] $*"; }
fail() { echo "[$(date '+%H:%M:%S')] FATAL: $*" >&2; exit 1; }

cleanup() {
    local result=$?
    trap - EXIT
    if [ -n "$VERIFY_DB" ]; then
        docker compose exec -T postgres dropdb -U thccb --if-exists "$VERIFY_DB" >/dev/null 2>&1 || true
    fi
    if [ "$result" -ne 0 ]; then
        if [ "$SCHEMA_ATTEMPTED" = 1 ]; then
            # A failed migration/health check cannot safely restart the old
            # writer against an unknown schema. Require explicit recovery.
            docker compose stop backend >/dev/null 2>&1 || true
            log "Migration started; backend remains stopped for operator recovery"
        elif [ "$BACKEND_STOPPED" = 1 ]; then
            # Pull does not replace the old container; start its old image.
            log "Pre-migration failure; restarting the previous backend container"
            docker compose start backend || true
        fi
    fi
    exit "$result"
}
trap cleanup EXIT

health_check() {
    log "Running health check ($HEALTH_RETRIES attempts)..."
    for i in $(seq 1 "$HEALTH_RETRIES"); do
        if curl -sf --max-time "$HEALTH_TIMEOUT" "$HEALTH_URL" > /dev/null 2>&1; then
            log "  Health check passed (attempt $i)"
            return 0
        fi
        log "  attempt $i/$HEALTH_RETRIES failed, waiting 3s..."
        sleep 3
    done
    return 1
}

echo "===================================="
echo "  TouhouCCB Deploy (Docker)"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "===================================="

# ── 0. 环境校验 ──
log "[0/5] Validating environment..."
[ -f "$PROJECT_ROOT/.env" ] || fail ".env not found (copy from .env.example)"
command -v docker >/dev/null 2>&1    || fail "docker not installed"
docker compose version >/dev/null 2>&1 || fail "docker compose plugin not installed"

[[ "${EXPECTED_BUILD_SHA:-}" =~ ^[0-9a-f]{40}$ ]] || fail "EXPECTED_BUILD_SHA must be a full commit SHA"
[ -n "${BACKEND_IMAGE:-}" ] || fail "BACKEND_IMAGE must name this commit's immutable image"
[ "${BACKEND_IMAGE##*:}" = "$EXPECTED_BUILD_SHA" ] || fail "BACKEND_IMAGE tag does not match EXPECTED_BUILD_SHA"

if grep -q "^SECRET_KEY=change_me" "$PROJECT_ROOT/.env" 2>/dev/null; then
    fail "SECRET_KEY is still the default value"
fi

# ── 1. 拉取新镜像，旧 backend 仍可服务 ──
# 只拉 backend：postgres 使用本地已有的镜像，避免 Docker Hub 抽风时卡死整个部署。
# 如需升级 postgres，手动 `docker compose pull postgres && docker compose up -d postgres`。
log "[1/5] Pulling latest backend image..."
docker compose pull backend

# 以应用实际连接串为准；DATABASE_URL 会覆盖 DB_BACKEND。部署脚本的备份和
# schema 检查只支持本 compose 的固定 PG 库或默认挂载的 SQLite 文件。
# 其它目标必须另备对应的安全部署流程，绝不能查错库后执行迁移。
DB_BACKEND=$(docker compose run --rm --no-deps -T backend python - <<'PY' | tail -n 1
from pathlib import Path
from sqlalchemy.engine import make_url
from app.core.config import settings

url = make_url(settings.build_db_url())
backend = url.get_backend_name()
if backend == 'postgresql':
    if (url.host, url.port or 5432, url.username, url.database) != (
        'postgres', 5432, 'thccb', 'thccb'
    ):
        raise SystemExit('Unsupported PostgreSQL target for this deploy script')
    print('postgres')
elif backend == 'sqlite':
    if Path(url.database or '').resolve() != Path('/app/data/thccb.db'):
        raise SystemExit('Unsupported SQLite target for this deploy script')
    print('sqlite')
else:
    raise SystemExit('Unsupported database backend for this deploy script')
PY
)
case "$DB_BACKEND" in
    postgres|sqlite) log "  Effective database backend: $DB_BACKEND" ;;
    *) fail "Could not determine supported effective database target" ;;
esac

# ── 2. 起 postgres、停 backend 写入口并制作可恢复备份 ──
log "[2/5] Preparing database and stopping economic writers..."
docker compose up -d postgres
for i in $(seq 1 15); do
    if docker compose exec -T postgres pg_isready -U thccb >/dev/null 2>&1; then
        log "  Postgres ready (attempt $i)"
        break
    fi
    sleep 1
    [ "$i" = "15" ] && fail "Postgres did not become ready"
done

# Read-only classification happens before the serving writer is stopped.
log "  Checking supported business schema..."
SCHEMA_STATE=$(docker compose run --rm --no-deps -T backend python -m app.core.schema | tail -n 1)
case "$SCHEMA_STATE" in
    empty|upgradeable) log "  Schema: $SCHEMA_STATE" ;;
    *) fail "Unsupported schema; use the approved business reset procedure" ;;
esac

if [ -n "$(docker compose ps --status running -q backend)" ]; then
    BACKEND_STOPPED=1
    docker compose stop backend
fi

mkdir -p "$BACKUP_DIR"
TIMESTAMP=$(date -u '+%Y%m%d_%H%M%S')_$$
if [ "$DB_BACKEND" = "postgres" ]; then
    BACKUP_PATH="$BACKUP_DIR/thccb_${TIMESTAMP}.dump"
    # A failed or empty backup aborts deployment. Never migrate without one.
    set -o noclobber
    docker compose exec -T postgres pg_dump -Fc -U thccb thccb > "$BACKUP_PATH"
    test -s "$BACKUP_PATH" || fail "PostgreSQL backup is empty"
    docker compose exec -T postgres pg_restore --list < "$BACKUP_PATH" > "$BACKUP_PATH.toc"
    test -s "$BACKUP_PATH.toc" || fail "PostgreSQL backup inventory is empty"
    VERIFY_DB="thccb_deploy_verify_${TIMESTAMP}"
    docker compose exec -T postgres createdb -U thccb --template=template0 "$VERIFY_DB"
    docker compose exec -T postgres pg_restore --exit-on-error -U thccb -d "$VERIFY_DB" < "$BACKUP_PATH"
    docker compose exec -T postgres dropdb -U thccb "$VERIFY_DB"
    VERIFY_DB=''
    log "  Full backup restore verified: $BACKUP_PATH"
elif [ "$DB_BACKEND" = "sqlite" ]; then
    DB_PATH="$PROJECT_ROOT/backend/data/thccb.db"
    if [ -f "$DB_PATH" ]; then
        BACKUP_PATH="$BACKUP_DIR/thccb_${TIMESTAMP}.db"
        cp "$DB_PATH" "$BACKUP_PATH"
        cmp -s "$DB_PATH" "$BACKUP_PATH" || fail "SQLite backup differs from source"
        log "  SQLite backup verified: $BACKUP_PATH"
    else
        log "  No SQLite database yet; first-install schema bootstrap follows"
    fi
else
    fail "Unsupported DB_BACKEND: $DB_BACKEND"
fi

# ── 3. 停写状态下迁移 schema ──
log "[3/5] Migrating schema while backend writers are stopped..."
SCHEMA_ATTEMPTED=1
docker compose run --rm --no-deps -T backend alembic upgrade head

log "  Verifying audit replay against the stopped database..."
docker compose run --rm --no-deps -T backend python scripts/audit_verify.py

# ── 4. 重启 backend ──
log "[4/5] Starting backend..."
docker compose up -d backend
if [ "${THCCB_COOLIFY:-}" = 1 ]; then docker compose up -d frontend; fi

# ── 5. 健康检查 ──
log "[5/5] Waiting for backend to be ready..."
sleep 3
if ! health_check; then
    fail "Health check failed after deploy; inspect backend logs before recovery"
fi

RUNNING_SHA=$(docker inspect --format='{{range .Config.Env}}{{println .}}{{end}}' "$(docker compose ps -q backend)" \
    | sed -n 's/^APP_BUILD_SHA=//p')
[ "$RUNNING_SHA" = "$EXPECTED_BUILD_SHA" ] || fail "Running backend build SHA differs from deployed frontend"

NEW_IMAGE=$(docker inspect --format='{{.Image}}' "$(docker compose ps -q backend)" 2>/dev/null | cut -c8-19)
echo ""
echo "===================================="
echo "  Deploy complete!"
echo "  Image:    ${NEW_IMAGE:-unknown}"
echo "  DB:       ${DB_BACKEND}"
echo "  Logs:     docker compose logs -f backend"
echo "  Backup:   ${BACKUP_PATH:-none}"
echo "===================================="
