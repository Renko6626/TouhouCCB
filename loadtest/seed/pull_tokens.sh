#!/usr/bin/env bash
# pull_tokens.sh — 从生产服务器拉取 tokens.txt 到本机（开发机作为 k6 runner 时用）
#
# 用法：
#   PROD=deploy@<prod-ip> ./loadtest/seed/pull_tokens.sh
#   PROD=deploy@<prod-ip> PROD_DIR=/home/deploy/TouhouCCB ./loadtest/seed/pull_tokens.sh
#
# 前置：生产服务器上已经跑过 seed_users.sh + mint_tokens.sh

set -euo pipefail
cd "$(dirname "$0")/../.."

PROD="${PROD:?需要设置 PROD=user@host，例如：PROD=deploy@1.2.3.4 $0}"
PROD_DIR="${PROD_DIR:-/home/deploy/TouhouCCB}"

echo "[pull_tokens] 从 $PROD:$PROD_DIR/loadtest/tokens.txt 拉取..."
scp "$PROD:$PROD_DIR/loadtest/tokens.txt" loadtest/tokens.txt

LINES=$(wc -l < loadtest/tokens.txt)
echo "[pull_tokens] 拉到 $LINES 个 token → loadtest/tokens.txt"
echo
echo "⚠️  tokens.txt 含 SECRET_KEY 签出的 JWT，泄露 = 任意账户接管。"
echo "    压测完立即删除：rm loadtest/tokens.txt"
