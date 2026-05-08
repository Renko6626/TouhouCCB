#!/usr/bin/env bash
# backup_before.sh — 压测前 pg_dump 全库，gzip 压缩到 backups/
#
# 输出：backups/loadtest_pre_<UTC时间>.sql.gz
# 是压测翻车后唯一的退路。失败立即退出，不允许「带病开测」。

set -euo pipefail
cd "$(dirname "$0")/../.."

mkdir -p backups
TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="backups/loadtest_pre_${TS}.sql.gz"

echo "[backup_before] dumping to $OUT ..."
docker compose exec -T postgres pg_dump -U thccb -d thccb --clean --if-exists \
  | gzip -9 > "$OUT"

# sanity check：gzip 至少要有几 KB，否则可能是空 dump
SIZE=$(stat -c%s "$OUT" 2>/dev/null || stat -f%z "$OUT")
if [ "$SIZE" -lt 1024 ]; then
  echo "ERR: backup file too small ($SIZE bytes), refusing to proceed" >&2
  exit 1
fi

echo "[backup_before] OK: $OUT ($SIZE bytes)"
echo
echo "若压测出问题，恢复命令："
echo "  gunzip < $OUT | docker compose exec -T postgres psql -U thccb -d thccb"
