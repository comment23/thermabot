-- 003_shadow (v0.1.9): shadow-vs-actual daily report. `samples.ts` is the PK (already
-- indexed); retention 120 d (D§2), pruned by the 00:05 rollup.
CREATE TABLE shadow_daily (day TEXT PRIMARY KEY,
  actual_heat_min REAL, actual_cool_min REAL, actual_cool_effort REAL, actual_fan_only_min REAL,
  actual_comfort_pct REAL, actual_violation_min REAL, sim_actual_runtime_min REAL,
  bot_heat_min REAL, bot_cool_min REAL, bot_cool_effort REAL,
  bot_comfort_pct REAL, bot_violation_min REAL,
  saved_min REAL, saved_pct REAL, energy_saved_pct REAL,
  model_version INTEGER, n_samples INTEGER NOT NULL DEFAULT 0, bot_steps INTEGER,
  coverage_pct REAL, note TEXT, created_at TEXT NOT NULL);
