#!/usr/bin/env bash
# pg.sh — 周期采样 postgres 状态，写入 loadtest/results/pg_<run>.log
#
# 采集：
#   每 2s 一帧 pg_stat_activity 中 state='active' 的查询计数 + 最长等待时长
#   每 2s 一帧 pg_locks 中 NOT granted 的锁数（队列深度）
# 跑前 reset、跑后 dump pg_stat_statements top 50。
#
# 用法：
#   ./loadtest/record/pg.sh start <run_tag>   # 重置 stats，开始周期采样
#   ./loadtest/record/pg.sh stop  <run_tag>   # 停止采样，dump pg_stat_statements
#
# 注意：pg_stat_statements 扩展需要在 postgres 容器里启用：
#   docker compose exec postgres psql -U thccb -c \
#     "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;"
#   还要确保 postgresql.conf 里有 shared_preload_libraries='pg_stat_statements'。
#   没启用也不影响 active/locks 采样，只是没有 top-50 SQL 报告。

set -uo pipefail
cd "$(dirname "$0")/../.."

ACTION="${1:-start}"
RUN_TAG="${2:-$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p loadtest/results

ACTIVE_LOG="loadtest/results/pg_active_${RUN_TAG}.log"
LOCKS_LOG="loadtest/results/pg_locks_${RUN_TAG}.log"
STMT_LOG="loadtest/results/pg_statements_${RUN_TAG}.log"
PID_FILE="loadtest/results/pg_${RUN_TAG}.pid"

psqlc() {
  docker compose exec -T postgres psql -U thccb -d thccb -tA -c "$1"
}

case "$ACTION" in
  start)
    echo "[pg] resetting pg_stat_statements (best effort)..."
    psqlc "SELECT pg_stat_statements_reset();" 2>/dev/null || \
      echo "  (pg_stat_statements 未启用，跳过)"

    echo "ts,active_count,longest_active_sec" > "$ACTIVE_LOG"
    echo "ts,not_granted_locks" > "$LOCKS_LOG"

    (
      while true; do
        TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        ACT=$(psqlc "SELECT COUNT(*), COALESCE(EXTRACT(EPOCH FROM MAX(now()-query_start)), 0)::int FROM pg_stat_activity WHERE state='active' AND pid<>pg_backend_pid();" 2>/dev/null || echo "0|0")
        AC=$(echo "$ACT" | awk -F'|' '{print $1}')
        AL=$(echo "$ACT" | awk -F'|' '{print $2}')
        echo "$TS,$AC,$AL" >> "$ACTIVE_LOG"

        LK=$(psqlc "SELECT COUNT(*) FROM pg_locks WHERE NOT granted;" 2>/dev/null || echo "0")
        echo "$TS,$LK" >> "$LOCKS_LOG"
        sleep 2
      done
    ) &
    echo $! > "$PID_FILE"
    echo "[pg] sampling started (pid=$(cat "$PID_FILE"))"
    echo "  active: $ACTIVE_LOG"
    echo "  locks:  $LOCKS_LOG"
    ;;

  stop)
    if [ -f "$PID_FILE" ]; then
      PID=$(cat "$PID_FILE")
      kill "$PID" 2>/dev/null && echo "[pg] sampler stopped (pid=$PID)"
      rm -f "$PID_FILE"
    else
      echo "[pg] no pid file at $PID_FILE — sampler may not be running"
    fi

    echo "[pg] dumping pg_stat_statements top 50 to $STMT_LOG..."
    psqlc "SELECT calls, total_exec_time::int AS total_ms, mean_exec_time::numeric(10,2) AS mean_ms, rows, query FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 50;" \
      > "$STMT_LOG" 2>/dev/null || echo "  (pg_stat_statements 不可用)"
    echo "[pg] done."
    ;;

  *)
    echo "用法: $0 {start|stop} [run_tag]" >&2
    exit 1
    ;;
esac
