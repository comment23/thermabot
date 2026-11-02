-- 004_efficiency (v0.5.0): per-day inputs of the efficiency curve, derived from `samples`
-- (outdoor temperature, runtime). Own table: daily_metrics/shadow_daily/samples are
-- untouched. Filled by the 00:05 rollup and a one-time, bounded start-up backfill.
-- Forward-only; every column but `day` is optional.
CREATE TABLE IF NOT EXISTS efficiency_daily (day TEXT PRIMARY KEY,
  mean_out_f REAL, hdd REAL, cdd REAL, heat_min REAL, cool_min REAL,
  n_samples INTEGER NOT NULL DEFAULT 0, out_hours INTEGER NOT NULL DEFAULT 0,
  coverage_pct REAL NOT NULL DEFAULT 0, base_f REAL NOT NULL DEFAULT 65,
  source TEXT NOT NULL DEFAULT 'samples', created_at TEXT NOT NULL);
