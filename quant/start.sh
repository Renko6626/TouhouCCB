#!/usr/bin/env bash
# 一键启动 thccb-quant trader + WebUI。
#
# 做的事：
#   1. 验证 venv + deps（fastapi/uvicorn 是 WebUI 新加的）
#   2. 验证 config.yaml + .env 存在
#   3. 优雅停老 trader（如在跑）+ 清残留
#   4. 起 tmux session 'quant' 跑 run.sh（watchdog）
#   5. 验证进程 + WebUI 端点 + SSE bootstrap
#   6. 打印监控/停机命令
#
# 任何步骤失败立即退出，不留半启动状态。
set -euo pipefail

cd "$(dirname "$0")"
QUANT_DIR="$(pwd)"
RED=$'\033[31m'; YEL=$'\033[33m'; GRN=$'\033[32m'; DIM=$'\033[2m'; NC=$'\033[0m'

log()   { echo "$@"; }
ok()    { echo "${GRN}✅${NC} $*"; }
warn()  { echo "${YEL}⚠️ ${NC} $*"; }
fail()  { echo "${RED}❌${NC} $*"; exit 1; }
hint()  { echo "${DIM}   $*${NC}"; }

# ─── 1. venv + deps ──────────────────────────────────────────
log "==> 检查 venv + deps"
if [ ! -d .venv ]; then
  fail ".venv 不存在。先建：python3 -m venv .venv && source .venv/bin/activate && pip install -e ."
fi
source .venv/bin/activate

# WebUI 新依赖；老 venv 可能没装
if ! python -c "import fastapi, uvicorn" 2>/dev/null; then
  log "  装 fastapi + uvicorn..."
  if command -v uv >/dev/null 2>&1; then
    uv pip install -q fastapi uvicorn
  else
    pip install -q fastapi uvicorn
  fi
fi

# 关键模块都能 import
python -c "from thccb_quant.trader import main_async; from thccb_quant.web import make_app" \
  || fail "trader / web module import 失败。先跑 'pip install -e .' 同步项目代码"
ok "venv + deps OK"

# ─── 2. config + env ─────────────────────────────────────────
log "==> 检查 config.yaml + .env"
if [ ! -f config.yaml ]; then
  fail "config.yaml 不存在。先 'cp config.example.yaml config.yaml' 然后启用策略"
fi
if [ ! -f .env ]; then
  fail ".env 不存在。需要填 THCCB_BASE_URL / THCCB_ACCESS_TOKEN / THCCB_REFRESH_TOKEN"
fi

# 看一眼启用的策略，给个 sanity 提示
enabled_count=$(grep -c "enabled: true" config.yaml || true)
if [ "$enabled_count" -eq 0 ]; then
  fail "config.yaml 里没有任何 enabled: true 的策略，跑起来也没用"
fi
ok "config + env 就绪（启用策略数：$enabled_count）"

# ─── 3. 优雅停老 trader ──────────────────────────────────────
log "==> 检查老 trader"
mkdir -p state logs
if pgrep -af thccb-quant 2>/dev/null | grep -v "/app/" | grep -v grep > /dev/null; then
  warn "有老 trader 在跑，优雅停..."
  touch state/KILL
  for i in $(seq 1 25); do
    if ! pgrep -af thccb-quant 2>/dev/null | grep -v "/app/" | grep -v grep > /dev/null; then
      ok "老 trader 已退出（${i}s）"
      break
    fi
    sleep 1
  done
  if pgrep -af thccb-quant 2>/dev/null | grep -v "/app/" | grep -v grep > /dev/null; then
    warn "优雅停超时，硬杀 tmux session"
    tmux kill-session -t quant 2>/dev/null || true
    sleep 2
  fi
fi
rm -f state/KILL
tmux kill-session -t quant 2>/dev/null || true

# ─── 4. 启动 ────────────────────────────────────────────────
log "==> 起 trader + WebUI"
tmux new -d -s quant 'bash run.sh'
sleep 6

PID=$(pgrep -af thccb-quant 2>/dev/null | grep -v "/app/" | grep -v grep | awk '{print $1}' | head -1 || true)
[ -z "$PID" ] && fail "trader 没起来！tmux attach -t quant 看错误"

ok "trader 起来了 PID=$PID（uptime=$(ps -o etime= -p "$PID" 2>/dev/null | tr -d ' ')）"

# ─── 5. 验证 ────────────────────────────────────────────────
log "==> 验证 WebUI"

# 从 config 读 web port（缺则默认 8765）
WEB_PORT=$(python -c "
import yaml
with open('config.yaml') as f: c = yaml.safe_load(f)
print((c.get('web') or {}).get('port', 8765))
" 2>/dev/null || echo 8765)

# 等 uvicorn 实际监听上（最多 8s）
for i in $(seq 1 16); do
  if curl -sf -o /dev/null -m 1 "http://127.0.0.1:$WEB_PORT/api/status" 2>/dev/null; then
    break
  fi
  sleep 0.5
done

STATUS_JSON=$(curl -sf -m 3 "http://127.0.0.1:$WEB_PORT/api/status" 2>/dev/null || true)
if [ -z "$STATUS_JSON" ]; then
  warn "WebUI 端口 $WEB_PORT 没响应（trader 仍在跑，可能 config 关了 web）"
else
  STRATEGY_COUNT=$(echo "$STATUS_JSON" | python -c "import json,sys;print(json.load(sys.stdin)['strategies_count'])" 2>/dev/null || echo "?")
  SSE_ACTIVE=$(echo "$STATUS_JSON" | python -c "import json,sys;print(json.load(sys.stdin)['sse']['active'])" 2>/dev/null || echo "?")
  DRY=$(echo "$STATUS_JSON" | python -c "import json,sys;print(json.load(sys.stdin)['dry_run'])" 2>/dev/null || echo "?")
  ok "WebUI on http://127.0.0.1:$WEB_PORT (strategies=$STRATEGY_COUNT  sse_active=$SSE_ACTIVE  dry_run=$DRY)"
fi

# 验证 SSE bootstrap 真的拿到 snapshot（最多再等 5s 防 startup race）
SSE_BOOT_OK=0
for _ in $(seq 1 10); do
  # 只看 trader 当前进程启动之后的日志行（用 PID 标记不容易，简单用 5s 窗口看最近 bootstrap）
  if tail -50 logs/system.jsonl 2>/dev/null | grep -q '"event":"sse_snapshot_bootstrapped"'; then
    SSE_BOOT_OK=1; break
  fi
  sleep 0.5
done
if [ "$SSE_BOOT_OK" = 1 ]; then
  ok "SSE snapshot bootstrap 成功"
else
  warn "5s 内没看到 sse_snapshot_bootstrapped，可能 SSE 连不上 backend；查 logs/system.jsonl"
fi

# ─── 6. 操作面板 ────────────────────────────────────────────
echo
log "==> 跑起来了。常用命令："
hint "WebUI:           http://127.0.0.1:$WEB_PORT"
hint "进 tmux:         tmux attach -t quant   (Ctrl-B D 退出不杀)"
hint "看实时日志:      tail -f $QUANT_DIR/logs/system.jsonl | jq -c ."
hint "看策略事件:      tail -f $QUANT_DIR/logs/system.jsonl | jq -c 'select(.event|startswith(\"meanrev_\") or startswith(\"order_\") or startswith(\"sse_trade_dedup\"))'"
hint "查决策:          sqlite3 $QUANT_DIR/state/quant.db \"SELECT ts,action,substr(reason,1,40) FROM decisions ORDER BY id DESC LIMIT 10\""
hint "优雅停:          touch $QUANT_DIR/state/KILL"
hint "硬停:            tmux kill-session -t quant"
