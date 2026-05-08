#!/usr/bin/env bash
# cleanup.sh — 把 cleanup.sql 喂进 postgres 容器；事务包裹，失败自动回滚。

set -euo pipefail
cd "$(dirname "$0")/../.."

SQL="loadtest/cleanup/cleanup.sql"
if [ ! -f "$SQL" ]; then
  echo "ERR: $SQL not found" >&2
  exit 1
fi

echo "[cleanup] 即将删除所有 casdoor_id LIKE 'loadtest:%' 的用户和 [LT] 开头的市场。"
read -r -p "确认继续？(yes/N) " ans
if [ "$ans" != "yes" ]; then
  echo "已取消。"
  exit 0
fi

cat "$SQL" | docker compose exec -T postgres psql -U thccb -d thccb -v ON_ERROR_STOP=1
echo "[cleanup] done."
