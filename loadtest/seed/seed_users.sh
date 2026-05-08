#!/usr/bin/env bash
# seed_users.sh — 创建 N 个压测虚拟用户，casdoor_id='loadtest:NNN' 严格隔离
#
# 用法：
#   ./loadtest/seed/seed_users.sh           # 默认 200 个
#   N=300 ./loadtest/seed/seed_users.sh     # 自定义数量
#   INITIAL_CASH=100000 ./loadtest/seed/seed_users.sh
#
# 必须在 docker-compose.yml 同级目录运行（脚本会自己 cd）。
# 所有用户用 casdoor_id LIKE 'loadtest:%' 标记，cleanup.sql 据此回收。

set -euo pipefail

cd "$(dirname "$0")/../.."

N="${N:-200}"
INITIAL_CASH="${INITIAL_CASH:-100000}"

if ! [[ "$N" =~ ^[0-9]+$ ]] || [ "$N" -lt 1 ] || [ "$N" -gt 5000 ]; then
  echo "ERR: N must be 1..5000, got: $N" >&2
  exit 1
fi

echo "[seed_users] inserting $N loadtest users with cash=$INITIAL_CASH..."

# 生成 SQL：ON CONFLICT DO NOTHING 让脚本可重入
{
  echo "BEGIN;"
  for i in $(seq 1 "$N"); do
    printf -v idx '%04d' "$i"
    cat <<SQL
INSERT INTO "user" (casdoor_id, username, email, is_active, is_superuser, cash, debt)
VALUES ('loadtest:${idx}', 'loadtest_${idx}', 'loadtest_${idx}@invalid.local', true, false, ${INITIAL_CASH}, 0)
ON CONFLICT (casdoor_id) DO NOTHING;
SQL
  done
  echo "COMMIT;"
  echo "SELECT COUNT(*) AS loadtest_users FROM \"user\" WHERE casdoor_id LIKE 'loadtest:%';"
} | docker compose exec -T postgres psql -U thccb -d thccb -v ON_ERROR_STOP=1

echo "[seed_users] done."
