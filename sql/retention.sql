-- Indicator Dashboard SQLite schema and retention maintenance.
-- Run against ./data/indicator.db:
--   sqlite3 data/indicator.db < sql/retention.sql
--
-- Retention defaults:
--   scans: 90 days
--   alerts: 180 days
--   signal_outcomes: 365 days
--   inactive watchlist rows: 365 days from added_at

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY,
    symbol VARCHAR(16) NOT NULL,
    side VARCHAR(16) NOT NULL,
    score INTEGER NOT NULL,
    max_score INTEGER NOT NULL,
    grade VARCHAR(32) NOT NULL,
    price FLOAT NOT NULL,
    reasons TEXT,
    warnings TEXT,
    created_at VARCHAR(64) NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_scans_id ON scans (id);
CREATE INDEX IF NOT EXISTS ix_scans_symbol ON scans (symbol);
CREATE INDEX IF NOT EXISTS ix_scans_created_at ON scans (created_at);
CREATE INDEX IF NOT EXISTS ix_scans_symbol_created_at ON scans (symbol, created_at);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    symbol VARCHAR(16) NOT NULL,
    side VARCHAR(16) NOT NULL,
    score INTEGER NOT NULL,
    price FLOAT NOT NULL,
    reasons TEXT,
    created_at VARCHAR(64) NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_alerts_id ON alerts (id);
CREATE INDEX IF NOT EXISTS ix_alerts_symbol ON alerts (symbol);
CREATE INDEX IF NOT EXISTS ix_alerts_created_at ON alerts (created_at);
CREATE INDEX IF NOT EXISTS ix_alerts_symbol_created_at ON alerts (symbol, created_at);

CREATE TABLE IF NOT EXISTS watchlist (
    symbol VARCHAR(16) PRIMARY KEY,
    source VARCHAR(32) NOT NULL DEFAULT 'user',
    active BOOLEAN NOT NULL DEFAULT 1,
    added_at VARCHAR(64) NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_watchlist_symbol ON watchlist (symbol);
CREATE INDEX IF NOT EXISTS ix_watchlist_active ON watchlist (active);

CREATE TABLE IF NOT EXISTS signal_outcomes (
    id INTEGER PRIMARY KEY,
    symbol VARCHAR(16) NOT NULL,
    side VARCHAR(16) NOT NULL,
    score INTEGER NOT NULL,
    outcome VARCHAR(32),
    notes TEXT,
    created_at VARCHAR(64) NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_signal_outcomes_id ON signal_outcomes (id);
CREATE INDEX IF NOT EXISTS ix_signal_outcomes_symbol ON signal_outcomes (symbol);
CREATE INDEX IF NOT EXISTS ix_signal_outcomes_created_at ON signal_outcomes (created_at);
CREATE INDEX IF NOT EXISTS ix_signal_outcomes_symbol_created_at ON signal_outcomes (symbol, created_at);

-- Retention cleanup.
-- SQLite datetime() treats timestamps without an explicit offset as UTC-like date strings for this purpose.
DELETE FROM scans
WHERE datetime(created_at) < datetime('now', '-90 days');

DELETE FROM alerts
WHERE datetime(created_at) < datetime('now', '-180 days');

DELETE FROM signal_outcomes
WHERE datetime(created_at) < datetime('now', '-365 days');

DELETE FROM watchlist
WHERE active = 0
  AND datetime(added_at) < datetime('now', '-365 days');

-- Keep the database compact after deleting old scan/cache-like rows.
PRAGMA optimize;
VACUUM;
