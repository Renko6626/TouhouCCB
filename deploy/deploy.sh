#!/usr/bin/env bash
# TouhouCCB Docker 部署脚本
# 用法：bash deploy/deploy.sh
#
# CI 已完成：Docker 镜像构建推送到 GHCR，前端 dist/ 已 rsync 到位。
# 本脚本负责：备份数据库 → 拉取新镜像 → 重启容器 → 健康检查。

set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT_ROOT="$(pwd)"
BACKUP_DIR="$PROJECT_ROOT/backups"
HEALTH_URL="http://127.0.0.1:8004/api/v1/market/list"
HEALTH_TIMEOUT=10
# 20×3s=60s 窗口：给冷启动期（init_db + auto_migrate + admin setup + APS）留余地
HEALTH_RETRIES=20

# ── 工具函数 ──

log()  { echo "[$(date '+%H:%M:%S')] $*"; }
fail() { echo "[$(date '+%H:%M:%S')] FATAL: $*" >&2; exit 1; }

rollback() {
    log "Rolling back — restarting previous containers..."
    docker compose up -d 2>/dev/null || true
    fail "Deploy failed. Check: docker compose logs backend"
}

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

# 检测当前数据库后端（从 .env 读取）
get_db_backend() {
    grep -oP '^DB_BACKEND=\K\S+' "$PROJECT_ROOT/.env" 2>/dev/null || echo "sqlite"
}

echo "===================================="
echo "  TouhouCCB Deploy (Docker)"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "===================================="

# ── 0. 环境校验 ──
log "[0/4] Validating environment..."
[ -f "$PROJECT_ROOT/.env" ] || fail ".env not found (copy from .env.example)"
command -v docker >/dev/null 2>&1    || fail "docker not installed"
docker compose version >/dev/null 2>&1 || fail "docker compose plugin not installed"

if grep -q "^SECRET_KEY=change_me" "$PROJECT_ROOT/.env" 2>/dev/null; then
    fail "SECRET_KEY is still the default value"
fi

# ── 1. 备份数据库 ──
log "[1/4] Backing up database..."
mkdir -p "$BACKUP_DIR"
TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
DB_BACKEND=$(get_db_backend)

if [ "$DB_BACKEND" = "postgres" ]; then
    # PostgreSQL: pg_dump 到 SQL 文件
    if docker compose ps -q postgres 2>/dev/null | grep -q .; then
        docker compose exec -T postgres pg_dump -U thccb thccb \
            > "$BACKUP_DIR/thccb_${TIMESTAMP}.sql" 2>/dev/null \
            && log "  Backed up to backups/thccb_${TIMESTAMP}.sql" \
            || log "  Warning: pg_dump failed, continuing without backup"
        # 保留最近 10 个备份
        ls -t "$BACKUP_DIR"/thccb_*.sql 2>/dev/null | tail -n +11 | xargs -r rm --
    else
        log "  PostgreSQL not running, skipping backup (first deploy?)"
    fi
elif [ -f "$PROJECT_ROOT/backend/data/thccb.db" ]; then
    # SQLite: 直接复制
    cp "$PROJECT_ROOT/backend/data/thccb.db" "$BACKUP_DIR/thccb_${TIMESTAMP}.db"
    log "  Backed up to backups/thccb_${TIMESTAMP}.db"
    ls -t "$BACKUP_DIR"/thccb_*.db 2>/dev/null | tail -n +11 | xargs -r rm --
else
    log "  No existing database, skipping backup"
fi

# ── 2. 拉取新镜像 ──
# 只拉 backend：postgres 使用本地已有的镜像，避免 Docker Hub 抽风时卡死整个部署。
# 如需升级 postgres，手动 `docker compose pull postgres && docker compose up -d postgres`。
log "[2/5] Pulling latest backend image..."
docker compose pull backend || rollback

# ── 3. 起 postgres + 跑 alembic 迁移 ──
# 用 `docker compose run --rm --no-deps backend alembic upgrade head` 起一个临时容器跑迁移，
# 这样不跟主 backend 容器 lifespan（init_db + auto_migrate）并发抢同一张表的 DDL 锁。
# 时机：在 backend 重启之前，保证 backend 启动时 schema 已就绪、不会因新加列报 5xx。
# 失败回滚：alembic 失败 = 镜像新版与 prod schema 不兼容，必须停下，rollback 保留旧 backend。
log "[3/5] Starting postgres and applying alembic migrations..."
docker compose up -d postgres
for i in $(seq 1 15); do
    if docker compose exec -T postgres pg_isready -U thccb >/dev/null 2>&1; then
        log "  Postgres ready (attempt $i)"
        break
    fi
    sleep 1
    [ "$i" = "15" ] && rollback
done
if ! docker compose run --rm --no-deps backend alembic upgrade head; then
    log "alembic upgrade head failed"
    rollback
fi

# ── 4. 重启 backend ──
log "[4/5] Starting backend..."
docker compose up -d backend || rollback

# ── 5. 健康检查 ──
log "[5/5] Waiting for backend to be ready..."
sleep 3
if ! health_check; then
    log "Health check failed after deploy"
    rollback
fi

NEW_IMAGE=$(docker inspect --format='{{.Image}}' thccb-backend 2>/dev/null | cut -c8-19)
echo ""
echo "===================================="
echo "  Deploy complete!"
echo "  Image:    ${NEW_IMAGE:-unknown}"
echo "  DB:       ${DB_BACKEND}"
echo "  Logs:     docker compose logs -f backend"
echo "  Backup:   backups/thccb_${TIMESTAMP}.$( [ "$DB_BACKEND" = "postgres" ] && echo sql || echo db )"
echo "===================================="
