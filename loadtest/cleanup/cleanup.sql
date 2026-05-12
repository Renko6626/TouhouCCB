-- cleanup.sql — 删除所有 loadtest 数据，按 FK 依赖自顶向下删。
--
-- 触发条件：casdoor_id LIKE 'loadtest:%' 的 user，title LIKE '[LT]%' 的 market。
-- 不会动真实用户、真实市场。
--
-- 顺序（FK 依赖）：
--   transaction → position → outcome / user → market
--
-- 注意：
--   - 用 BEGIN/COMMIT 包裹，任何一步失败回滚。
--   - 不用 TRUNCATE / DROP（CLAUDE.md 红线）。
--   - 兑换码 / loan 相关表暂时只删 FK 引用 user 的部分。

BEGIN;

-- 1) 先删 redemption 链（如果跑过涉及 redemption 的测）
DELETE FROM redemption_transaction
WHERE user_id IN (SELECT id FROM "user" WHERE casdoor_id LIKE 'loadtest:%');

UPDATE redemption_code
SET bought_by_user_id = NULL, bought_at = NULL, marked_used_by_user_at = NULL, status = 'available'
WHERE bought_by_user_id IN (SELECT id FROM "user" WHERE casdoor_id LIKE 'loadtest:%');

-- 2) loadtest 用户的全部交易记录
DELETE FROM transaction
WHERE user_id IN (SELECT id FROM "user" WHERE casdoor_id LIKE 'loadtest:%');

-- 3) loadtest 市场上所有 transaction（其他用户在 [LT] 市场上下的单也清掉）
DELETE FROM transaction
WHERE outcome_id IN (
  SELECT o.id FROM outcome o JOIN market m ON o.market_id=m.id
  WHERE m.title LIKE '[LT]%'
);

-- 4) loadtest 用户的 position
DELETE FROM position
WHERE user_id IN (SELECT id FROM "user" WHERE casdoor_id LIKE 'loadtest:%');

-- 5) loadtest 市场上的 position（防同上）
DELETE FROM position
WHERE outcome_id IN (
  SELECT o.id FROM outcome o JOIN market m ON o.market_id=m.id
  WHERE m.title LIKE '[LT]%'
);

-- 6) loadtest 市场的 outcome（必须先 NULL out market.winning_outcome_id 的 FK）
UPDATE market SET winning_outcome_id = NULL WHERE title LIKE '[LT]%';
DELETE FROM outcome
WHERE market_id IN (SELECT id FROM market WHERE title LIKE '[LT]%');

-- 7) loadtest 市场本身
DELETE FROM market WHERE title LIKE '[LT]%';

-- 8) loadtest 用户
DELETE FROM "user" WHERE casdoor_id LIKE 'loadtest:%';

-- 校验
DO $$
DECLARE
  u_count INT;
  m_count INT;
BEGIN
  SELECT COUNT(*) INTO u_count FROM "user" WHERE casdoor_id LIKE 'loadtest:%';
  SELECT COUNT(*) INTO m_count FROM market WHERE title LIKE '[LT]%';
  IF u_count > 0 OR m_count > 0 THEN
    RAISE EXCEPTION 'cleanup 失败：还有 % 个 loadtest user / % 个 [LT] market 残留', u_count, m_count;
  END IF;
END $$;

COMMIT;

\echo
\echo === cleanup 完成。剩余 loadtest 痕迹（应为 0）：===
SELECT 'users' AS what, COUNT(*) FROM "user" WHERE casdoor_id LIKE 'loadtest:%'
UNION ALL
SELECT 'markets', COUNT(*) FROM market WHERE title LIKE '[LT]%';
