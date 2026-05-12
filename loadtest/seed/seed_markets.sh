#!/usr/bin/env bash
# seed_markets.sh — 起若干压测市场（标题前缀 [LT] 方便清理）
#
# 5 个市场覆盖热点 / 冷门 / 高 b / 低 b / 多 outcome：
#   #1  [LT] HOT_2OPT       b=200  2 outcomes (集中打这个测最坏锁排队)
#   #2  [LT] COLD_2OPT      b=100  2 outcomes
#   #3  [LT] HIB_4OPT       b=500  4 outcomes (高 b → 价格变动小)
#   #4  [LT] LOWB_2OPT      b=50   2 outcomes (低 b → 单笔大单滑点剧烈)
#   #5  [LT] MULTI_8OPT     b=200  8 outcomes (LMSR exp 项更多 → CPU 压力)
#
# market.title 不是 unique 索引，所以脚本不可重入：已存在 [LT] 市场时直接退出。
# 重跑请先用 cleanup.sql 清掉旧的。

set -euo pipefail
cd "$(dirname "$0")/../.."

echo "[seed_markets] checking existing [LT] markets..."

EXISTING=$(docker compose exec -T postgres psql -U thccb -d thccb -tAc \
  "SELECT COUNT(*) FROM market WHERE title LIKE '[LT]%';" | tr -d '[:space:]')

if [ "$EXISTING" != "0" ]; then
  echo "ERR: 已经有 $EXISTING 个 [LT] 市场存在。先跑 cleanup 再 seed。" >&2
  echo "     loadtest/cleanup/cleanup_markets.sh" >&2
  exit 1
fi

echo "[seed_markets] inserting 5 loadtest markets..."

docker compose exec -T postgres psql -U thccb -d thccb -v ON_ERROR_STOP=1 <<'SQL'
BEGIN;

-- 一次性建市场，拿到 id 立刻插对应 outcomes
WITH new_markets AS (
  INSERT INTO market (title, description, liquidity_b, status, tags, created_at)
  VALUES
    ('[LT] HOT_2OPT',    'loadtest hot market — concentrate fire here', 200, 'trading', 'loadtest,hot', NOW()),
    ('[LT] COLD_2OPT',   'loadtest cold market',                        100, 'trading', 'loadtest',     NOW()),
    ('[LT] HIB_4OPT',    'loadtest high-b market',                      500, 'trading', 'loadtest',     NOW()),
    ('[LT] LOWB_2OPT',   'loadtest low-b market — wide slippage',        50, 'trading', 'loadtest',     NOW()),
    ('[LT] MULTI_8OPT',  'loadtest 8-way market',                       200, 'trading', 'loadtest',     NOW())
  RETURNING id, title
),
labels_per_market AS (
  SELECT
    nm.id AS market_id,
    CASE nm.title
      WHEN '[LT] HOT_2OPT'   THEN ARRAY['YES','NO']
      WHEN '[LT] COLD_2OPT'  THEN ARRAY['YES','NO']
      WHEN '[LT] HIB_4OPT'   THEN ARRAY['A','B','C','D']
      WHEN '[LT] LOWB_2OPT'  THEN ARRAY['UP','DOWN']
      WHEN '[LT] MULTI_8OPT' THEN ARRAY['O1','O2','O3','O4','O5','O6','O7','O8']
    END AS labels
  FROM new_markets nm
)
INSERT INTO outcome (market_id, label, total_shares)
SELECT lpm.market_id, lab, 0
FROM labels_per_market lpm,
     unnest(lpm.labels) AS lab;

COMMIT;

\echo
\echo === loadtest markets ===
SELECT m.id, m.title, m.liquidity_b,
       (SELECT COUNT(*) FROM outcome o WHERE o.market_id=m.id) AS n_outcomes
FROM market m
WHERE m.title LIKE '[LT]%'
ORDER BY m.id;

\echo
\echo === loadtest outcomes ===
SELECT o.id, o.market_id, m.title, o.label
FROM outcome o
JOIN market m ON o.market_id=m.id
WHERE m.title LIKE '[LT]%'
ORDER BY o.market_id, o.id;
SQL

echo
echo "[seed_markets] done."
echo
echo "下一步：把 [LT] HOT_2OPT 的两个 outcome.id 写入 loadtest/scenarios/.env："
echo "  HOT_OUTCOME_YES=<id>"
echo "  HOT_OUTCOME_NO=<id>"
echo "  HOT_MARKET_ID=<id>"
