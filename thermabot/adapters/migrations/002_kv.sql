-- 002_kv: checksummed bot key/value state (e.g. last weekly summary slot).
CREATE TABLE bot_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL,
  updated_at TEXT NOT NULL, checksum TEXT NOT NULL);
