#!/usr/bin/env bash
# host.sh — 1s/次采样宿主机和容器资源，写入 loadtest/results/host_<run>.log
#
# 在 prod 机器上**和 k6 同时启动**，Ctrl-C 停止。
# 字段：timestamp, cpu_pct(总), mem_used_mb, load1, iowait_pct,
#       backend_cpu_pct, backend_mem_mb, postgres_cpu_pct, postgres_mem_mb
#
# 注意：2C 机器 CPU% 上限是 200%（k6 + uvicorn + postgres 抢核），
# load1 比 CPU% 更能说明排队，重点看 load1 和 iowait。

set -uo pipefail
cd "$(dirname "$0")/../.."

mkdir -p loadtest/results
RUN_TAG="${1:-$(date -u +%Y%m%dT%H%M%SZ)}"
OUT="loadtest/results/host_${RUN_TAG}.log"

echo "ts,cpu_total_pct,mem_used_mb,load1,iowait_pct,backend_cpu_pct,backend_mem_mb,postgres_cpu_pct,postgres_mem_mb" > "$OUT"

echo "[host] sampling 1Hz to $OUT (Ctrl-C to stop)..."

# ── helpers ──
get_cpu_pct() {
  # 取 /proc/stat 第一行 cpu，计算非 idle 占比
  awk '/^cpu / { tot=$2+$3+$4+$5+$6+$7+$8; idle=$5; print tot, idle }' /proc/stat
}
get_iowait_pct() {
  # 第 6 列 = iowait
  awk '/^cpu / { print $2+$3+$4+$5+$6+$7+$8, $6 }' /proc/stat
}

PREV_TOT=""; PREV_IDLE=""
PREV_TOT_IO=""; PREV_IO=""

while true; do
  TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  # 总 CPU%
  read TOT IDLE <<< "$(get_cpu_pct)"
  if [ -n "$PREV_TOT" ]; then
    DT=$(( TOT - PREV_TOT ))
    DI=$(( IDLE - PREV_IDLE ))
    if [ "$DT" -gt 0 ]; then
      CPU_PCT=$(awk -v dt="$DT" -v di="$DI" 'BEGIN { printf "%.1f", (1 - di/dt) * 100 }')
    else CPU_PCT="0.0"; fi
  else CPU_PCT="0.0"; fi
  PREV_TOT="$TOT"; PREV_IDLE="$IDLE"

  # iowait %
  read TOT_IO IO <<< "$(get_iowait_pct)"
  if [ -n "$PREV_TOT_IO" ]; then
    DT2=$(( TOT_IO - PREV_TOT_IO ))
    DI2=$(( IO - PREV_IO ))
    if [ "$DT2" -gt 0 ]; then
      IO_PCT=$(awk -v dt="$DT2" -v di="$DI2" 'BEGIN { printf "%.2f", (di/dt) * 100 }')
    else IO_PCT="0.00"; fi
  else IO_PCT="0.00"; fi
  PREV_TOT_IO="$TOT_IO"; PREV_IO="$IO"

  # mem
  MEM_MB=$(awk '/^MemTotal/{t=$2} /^MemAvailable/{a=$2} END{ printf "%d", (t-a)/1024 }' /proc/meminfo)
  LOAD1=$(awk '{print $1}' /proc/loadavg)

  # docker stats — 一次取所有容器，过滤目标
  STATS=$(docker stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}' 2>/dev/null || echo "")
  BCPU="0.0"; BMEM="0"
  PCPU="0.0"; PMEM="0"
  while IFS='|' read -r NAME CPU MEMU; do
    [ -z "$NAME" ] && continue
    PCT="${CPU%\%}"
    # MemUsage 形如 "123.4MiB / 1GiB"，取前半段
    MMB=$(echo "$MEMU" | awk -F'/' '{print $1}' | sed -E 's/[[:space:]]//g; s/MiB//; s/GiB/*1024/; s/KiB/\/1024/' | bc -l 2>/dev/null || echo "0")
    if [ "$NAME" = "thccb-backend" ]; then BCPU="$PCT"; BMEM="$MMB"; fi
    if [ "$NAME" = "thccb-postgres" ]; then PCPU="$PCT"; PMEM="$MMB"; fi
  done <<< "$STATS"

  printf '%s,%s,%s,%s,%s,%s,%.0f,%s,%.0f\n' \
    "$TS" "$CPU_PCT" "$MEM_MB" "$LOAD1" "$IO_PCT" \
    "$BCPU" "$BMEM" "$PCPU" "$PMEM" >> "$OUT"

  sleep 1
done
