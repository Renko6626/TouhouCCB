CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  strategy TEXT NOT NULL,
  outcome_id INTEGER NOT NULL,
  side TEXT NOT NULL,
  shares TEXT NOT NULL,
  price TEXT,
  cost TEXT,
  status TEXT NOT NULL,
  error TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_dedup
  ON orders (strategy, outcome_id, side, shares, ts);

CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  strategy TEXT NOT NULL,
  outcome_id INTEGER,
  action TEXT NOT NULL,
  reason TEXT,
  snapshot_json TEXT
);

CREATE TABLE IF NOT EXISTS daily_stats (
  date TEXT PRIMARY KEY,
  gross_turnover TEXT NOT NULL DEFAULT '0',
  net_pnl TEXT NOT NULL DEFAULT '0'
);
