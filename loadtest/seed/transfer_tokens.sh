#!/usr/bin/env bash
# transfer_tokens.sh — 把 tokens.txt 和 scenarios/ 传到远程压测机
#
# 用法：
#   REMOTE=user@1.2.3.4 ./loadtest/seed/transfer_tokens.sh
#   REMOTE=user@1.2.3.4 REMOTE_DIR=/tmp/lt ./loadtest/seed/transfer_tokens.sh
#
# 前置条件：
#   1) 已在 prod 上跑完 mint_tokens.sh（loadtest/tokens.txt 存在）
#   2) 远程机器已安装 k6 和 rsync
#   3) SSH key 已配置（或手动输密码）
#
# 安全：tokens.txt 里是 SECRET_KEY 签出的 JWT，scp 后立即测，测完两端都删。

set -euo pipefail
cd "$(dirname "$0")/../.."

REMOTE="${REMOTE:?需要设置 REMOTE=user@host，例如：REMOTE=deploy@1.2.3.4 $0}"
REMOTE_DIR="${REMOTE_DIR:-~/thccb-loadtest}"

if [ ! -s "loadtest/tokens.txt" ]; then
  echo "ERR: loadtest/tokens.txt 不存在或为空，先跑 mint_tokens.sh" >&2
  exit 1
fi

echo "[transfer] 目标：$REMOTE:$REMOTE_DIR"

# 在远程创建目录
ssh "$REMOTE" "mkdir -p $REMOTE_DIR/scenarios/lib"

# 传 tokens.txt（含敏感 JWT）
scp loadtest/tokens.txt "$REMOTE:$REMOTE_DIR/tokens.txt"
echo "[transfer] tokens.txt ✓"

# 传 scenarios/（含 lib/auth.js；用 rsync 增量，减少重传）
rsync -av --delete \
  loadtest/scenarios/ \
  "$REMOTE:$REMOTE_DIR/scenarios/"
echo "[transfer] scenarios/ ✓"

echo
echo "传输完成。在远程机器 $REMOTE 上运行："
echo
echo "  ssh $REMOTE"
echo "  cd $REMOTE_DIR"
echo "  export BASE_URL=https://your-instance.example.com"
echo "  export HOT_MARKET_ID=<填 id>"
echo "  export HOT_OUTCOME_YES=<填 id>"
echo "  export HOT_OUTCOME_NO=<填 id>"
echo "  k6 run scenarios/smoke.js                          # smoke 先验"
echo "  k6 run --out json=k6_trade.json scenarios/trade_ramp.js"
echo
echo "⚠️  测完后两端都删 tokens.txt："
echo "  本机：rm loadtest/tokens.txt"
echo "  远端：ssh $REMOTE rm $REMOTE_DIR/tokens.txt"
