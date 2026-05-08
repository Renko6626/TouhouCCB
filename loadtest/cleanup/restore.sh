#!/usr/bin/env bash
# restore.sh — 从 backup_before.sh 产出的 *.sql.gz 恢复全库。
#
# 用法：
#   ./loadtest/cleanup/restore.sh backups/loadtest_pre_<TS>.sql.gz
#
# 警告：这会**覆盖**当前数据库的全部内容（pg_dump 用了 --clean --if-exists）。
# 只在压测翻车、cleanup.sql 也救不回来时使用。

set -euo pipefail
cd "$(dirname "$0")/../.."

DUMP="${1:-}"
if [ -z "$DUMP" ] || [ ! -f "$DUMP" ]; then
  echo "用法: $0 backups/loadtest_pre_<TS>.sql.gz" >&2
  exit 1
fi

echo "[restore] 即将用 $DUMP 覆盖整个 thccb 库。"
echo "          (pg_dump 用了 --clean --if-exists，会先 DROP 旧对象)"
read -r -p "确认继续？(YES 全大写) " ans
if [ "$ans" != "YES" ]; then
  echo "已取消。"
  exit 0
fi

echo "[restore] 停 backend，避免恢复期间被打到..."
docker compose stop backend

echo "[restore] streaming $DUMP into postgres..."
gunzip < "$DUMP" | docker compose exec -T postgres psql -U thccb -d thccb -v ON_ERROR_STOP=1

echo "[restore] 重启 backend..."
docker compose start backend

echo "[restore] done. 检查健康："
sleep 5
curl -fsS http://127.0.0.1:8004/health || echo "  (backend 还没就绪，多等 10s 再试)"
