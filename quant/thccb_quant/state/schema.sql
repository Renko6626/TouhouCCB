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

CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trade_id INTEGER NOT NULL UNIQUE,
  ts TEXT NOT NULL,
  ingest_ts TEXT NOT NULL,
  market_id INTEGER NOT NULL,
  outcome_id INTEGER NOT NULL,
  side TEXT NOT NULL,
  shares TEXT NOT NULL,
  price TEXT NOT NULL,
  gross TEXT NOT NULL,
  fee TEXT NOT NULL,
  username TEXT,
  post_market_price TEXT NOT NULL,
  market_prices_post_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_market_ts ON trades(market_id, ts);

CREATE TABLE IF NOT EXISTS partial_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trade_id INTEGER NOT NULL UNIQUE,
  ts TEXT NOT NULL,
  market_id INTEGER NOT NULL,
  outcome_id INTEGER NOT NULL,
  side TEXT NOT NULL,
  shares TEXT NOT NULL,
  price TEXT NOT NULL,
  username TEXT,
  market_title TEXT,
  outcome_label TEXT
);
CREATE INDEX IF NOT EXISTS idx_partial_trades_market_ts ON partial_trades(market_id, ts);
