#!/usr/bin/env bash
# app_tail.sh — 流式 tail backend 日志，过滤慢请求和 5xx，写到 loadtest/results/。
#
# 中间件已经按 "%s %s %d %.1fms" 打 log，所以正则可以直接抓「>500ms」和「5xx」。
# 跑期间 tail 全程；Ctrl-C 停。

set -uo pipefail
cd "$(dirname "$0")/../.."

mkdir -p loadtest/results
RUN_TAG="${1:-$(date -u +%Y%m%dT%H%M%SZ)}"
OUT="loadtest/results/app_slow_${RUN_TAG}.log"

echo "[app_tail] writing to $OUT (Ctrl-C to stop)..."

# 用 awk 过滤：第 4 段毫秒 > 500，或 status 是 5xx
docker logs -f --since 1s thccb-backend 2>&1 | awk '
  /thccb\.access/ {
    # 形如: 2026-04-... INFO thccb.access POST /api/v1/market/buy 200 12.3ms
    # 倒数第二字段是 status，最后字段是 XXXms
    status = $(NF-1)
    ms_str = $(NF); sub(/ms$/, "", ms_str); ms = ms_str + 0
    if (ms > 500 || (status+0) >= 500) print
  }
  /ERROR|Traceback|invariant violated/ { print }
' | tee -a "$OUT"
