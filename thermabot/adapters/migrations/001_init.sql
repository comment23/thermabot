-- 001_init (D§2). Timestamps are UTC ISO-8601 text.
CREATE TABLE bot_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  kill_switch TEXT NOT NULL CHECK (kill_switch IN ('off','on')),
  run_mode TEXT NOT NULL CHECK (run_mode IN ('shadow','active')),
  shadow_started_at TEXT,
  disabled_since TEXT,
  preset_cleared INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL, updated_by TEXT NOT NULL,
  checksum TEXT NOT NULL
);

CREATE TABLE users (
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('owner','admin','member')),
  room TEXT, pw_hash TEXT NOT NULL, failed_logins INTEGER NOT NULL DEFAULT 0,
  locked_until TEXT, created_at TEXT NOT NULL
);
CREATE TABLE sessions (
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
  csrf TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, ip TEXT
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL, updated_by TEXT NOT NULL);
CREATE TABLE overrides (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('ui_target','ui_mode','ui_pause','external')),
  source TEXT NOT NULL, payload_json TEXT NOT NULL,
  started_at TEXT NOT NULL, expires_at TEXT NOT NULL, ended_at TEXT, end_reason TEXT
);
CREATE TABLE nudges (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
  room TEXT NOT NULL, delta_f REAL NOT NULL CHECK (delta_f BETWEEN -2 AND 2),
  started_at TEXT NOT NULL, expires_at TEXT NOT NULL, ended_at TEXT
);
CREATE TABLE plans (
  id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, trigger TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('optimized','fallback')),
  model_version INTEGER, forecast_source TEXT, steps_json TEXT NOT NULL,
  predicted_runtime_min REAL, min_score REAL
);
CREATE TABLE writes (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, service TEXT NOT NULL, entity_id TEXT NOT NULL,
  data_json TEXT NOT NULL, reason TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK (outcome IN ('sent','shadow','denied','failed')),
  guard TEXT, is_safety_restore INTEGER NOT NULL DEFAULT 0,
  context_id TEXT, readback_ok INTEGER, readback_ms INTEGER
);
CREATE INDEX writes_ts ON writes(ts);
CREATE TABLE off_windows (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL,
  planned_end TEXT NOT NULL, reevaluate_at TEXT NOT NULL, ended_at TEXT, end_reason TEXT);
CREATE TABLE audit (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, actor TEXT NOT NULL,
  ip TEXT, action TEXT NOT NULL, detail_json TEXT);
CREATE TABLE alerts (id INTEGER PRIMARY KEY, key TEXT NOT NULL, severity TEXT NOT NULL,
  opened_at TEXT NOT NULL, closed_at TEXT, last_sent_at TEXT, message TEXT NOT NULL,
  delivery TEXT);
CREATE UNIQUE INDEX alerts_open ON alerts(key) WHERE closed_at IS NULL;
CREATE TABLE samples (ts TEXT PRIMARY KEY, snapshot_json TEXT NOT NULL,
  bot_state TEXT NOT NULL, score REAL, aux_heat_flag INTEGER, stale_rooms TEXT);
CREATE TABLE models (version INTEGER PRIMARY KEY, fitted_at TEXT NOT NULL,
  params_json TEXT NOT NULL, mae_json TEXT NOT NULL, n_samples INTEGER, active INTEGER NOT NULL);
CREATE TABLE fan_experiments (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL,
  ended_at TEXT, gap_before_f REAL, gap_after_f REAL, outdoor_f REAL, season TEXT);
CREATE TABLE daily_metrics (day TEXT PRIMARY KEY,
  heat_min REAL, cool_min REAL, fan_only_min REAL, heat_effort REAL, cool_effort REAL,
  hdd REAL, cdd REAL, comfort_pct REAL, violation_min REAL, overrides INTEGER, bot_state TEXT);
