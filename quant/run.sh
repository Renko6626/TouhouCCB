#!/usr/bin/env bash
# 看门狗：崩了 30s 后重拉；exit 0（kill switch / FatalAuth）不重拉
set -u
cd "$(dirname "$0")"
source .venv/bin/activate

# ── 绕开本机 HTTP/SOCKS 代理（如 mihomo / clash） ──
# Trader 直连 thccb backend；走代理时 SSE 长连接会被代理静默截断（recv 端 CLOSE-WAIT，
# 客户端 zombie timeout 才察觉），导致 strategy 收不到 trade event。
# unset + NO_PROXY 双保险：unset 防 httpx 读 env_proxies；NO_PROXY 防别处再设回来。
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
export NO_PROXY="thccb.secret-sealing.club,127.0.0.1,localhost"
export no_proxy="$NO_PROXY"

while true; do
  echo "[$(date '+%F %T')] starting trader"
  python -m thccb_quant
  EXIT=$?
  if [ $EXIT -eq 0 ]; then
    echo "[$(date '+%F %T')] clean exit, watchdog quitting"
    break
  fi
  echo "[$(date '+%F %T')] crashed exit=$EXIT, restart in 30s"
  sleep 30
done
