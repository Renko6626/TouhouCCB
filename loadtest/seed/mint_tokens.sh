#!/usr/bin/env bash
# mint_tokens.sh — 在 backend 容器里跑 mint_tokens.py，结果写到 loadtest/tokens.txt
#
# 工作流：
#   1) docker cp 把 mint_tokens.py 复制进容器（ephemeral，不动镜像）
#   2) docker compose exec -T backend python /tmp/_loadtest_mint.py
#   3) 输出重定向到 loadtest/tokens.txt
#   4) 清理容器内的临时文件

set -euo pipefail
cd "$(dirname "$0")/../.."

OUT="loadtest/tokens.txt"
SRC="loadtest/seed/mint_tokens.py"

if [ ! -s "$SRC" ]; then
  echo "ERR: $SRC 不存在或为空" >&2
  exit 1
fi

# 拷进容器
docker cp "$SRC" thccb-backend:/tmp/_loadtest_mint.py

# 执行并捕获输出。stderr 直通终端，stdout 写到 tokens.txt
echo "[mint_tokens] running inside thccb-backend..."
docker compose exec -T backend python /tmp/_loadtest_mint.py > "$OUT"

# 清理
docker compose exec -T backend rm -f /tmp/_loadtest_mint.py

LINES=$(wc -l < "$OUT")
echo "[mint_tokens] wrote $LINES tokens to $OUT"
echo
echo "tokens.txt 里有 SECRET_KEY 签出的 access token，泄露 = 任意账户接管。"
echo "压测完立即删除：rm $OUT"
