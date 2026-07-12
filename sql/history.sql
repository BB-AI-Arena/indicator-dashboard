CREATE TABLE IF NOT EXISTS provider_errors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL,
  symbol TEXT,
  endpoint TEXT,
  error_message TEXT NOT NULL,
  error_type TEXT,
  retry_after_seconds INTEGER,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_provider_errors_provider_symbol_created_at
  ON provider_errors (provider, symbol, created_at);

CREATE TABLE IF NOT EXISTS backfill_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  status TEXT NOT NULL,
  symbols TEXT,
  intervals TEXT,
  period TEXT,
  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at TEXT,
  rows_inserted INTEGER DEFAULT 0,
  rows_updated INTEGER DEFAULT 0,
  chunks_total INTEGER DEFAULT 0,
  chunks_completed INTEGER DEFAULT 0,
  chunks_failed INTEGER DEFAULT 0,
  error_count INTEGER DEFAULT 0,
  message TEXT
);

CREATE TABLE IF NOT EXISTS backfill_chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  interval TEXT NOT NULL,
  start_timestamp TEXT NOT NULL,
  end_timestamp TEXT NOT NULL,
  status TEXT NOT NULL,
  provider TEXT,
  rows_inserted INTEGER DEFAULT 0,
  rows_updated INTEGER DEFAULT 0,
  error_message TEXT,
  started_at TEXT,
  finished_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(run_id) REFERENCES backfill_runs(id),
  UNIQUE(run_id, symbol, interval, start_timestamp, end_timestamp)
);

CREATE TABLE IF NOT EXISTS candles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  interval TEXT NOT NULL,
  timestamp INTEGER NOT NULL,
  open REAL NOT NULL,
  high REAL NOT NULL,
  low REAL NOT NULL,
  close REAL NOT NULL,
  volume REAL DEFAULT 0,
  provider TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(symbol, interval, timestamp)
);

CREATE INDEX IF NOT EXISTS ix_candles_symbol_interval_timestamp
  ON candles (symbol, interval, timestamp);
