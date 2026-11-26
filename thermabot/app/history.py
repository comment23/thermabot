"""v0.6.0: dashboard history chart data (GET /api/history?range=24h|7d|30d).

Read-only over `samples` (one row per minute). SQLite buckets the rows on a fixed grid of
at most MAX_POINTS buckets and averages them, so the server never decodes tens of thousands
of JSON documents in Python. The grid is dense: a bucket without samples is a null (a gap in
the chart), never a fake zero.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from thermabot.config import BlockName, Settings
from thermabot.core.comfort import block_band, room_band
from thermabot.core.types import Season

RANGES = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
MAX_POINTS = 500

_ACTION = "json_extract(snapshot_json, '$.tstat.hvac_action')"
_SQL = f"""
SELECT (CAST(strftime('%s', ts) AS INTEGER) - :t0) / :step AS b,
  COUNT(*) AS n,
  AVG(json_extract(snapshot_json, '$.tstat.temp')) AS indoor,
  AVG(COALESCE(json_extract(snapshot_json, '$.t_out'),
               json_extract(snapshot_json, '$.t_out_fc'))) AS outdoor,
  AVG(json_extract(snapshot_json, '$.tstat.heat_sp')) AS heat_sp,
  AVG(json_extract(snapshot_json, '$.tstat.cool_sp')) AS cool_sp,
  AVG(CASE {_ACTION} WHEN 'heating' THEN 1.0 ELSE 0.0 END) AS heat,
  AVG(CASE {_ACTION} WHEN 'cooling' THEN 1.0 ELSE 0.0 END) AS cool,
  AVG(CASE WHEN {_ACTION} = 'fan' THEN 1.0
           WHEN {_ACTION} IN ('heating', 'cooling') THEN 0.0
           WHEN json_extract(snapshot_json, '$.blower') > 0.5 THEN 1.0
           ELSE 0.0 END) AS fan
FROM samples WHERE ts >= :since AND ts < :until
GROUP BY b ORDER BY b
"""
TEMPS = ("indoor", "outdoor", "heat_sp", "cool_sp")
RUNS = ("heat", "cool", "fan")


def grid(span: timedelta) -> tuple[int, int]:
    """(bucket seconds, bucket count): whole minutes, count <= MAX_POINTS."""
    span_s = int(span.total_seconds())
    step = max(60, math.ceil(span_s / MAX_POINTS / 60) * 60)
    return step, math.ceil(span_s / step)


def series(conn: sqlite3.Connection, now: datetime, rng: str, units: str = "F") -> dict[str, Any]:
    """Columnar series for uPlot: `t` is the bucket start (epoch s, UTC). Temperatures stay
    °F (`units` is only the display preference); heat/cool/fan are run fractions 0..1."""
    step, n = grid(RANGES[rng])
    end = int(now.astimezone(UTC).replace(second=0, microsecond=0).timestamp()) + 60
    t0 = end - n * step

    def iso(s: int) -> str:
        return datetime.fromtimestamp(s, UTC).isoformat()

    out: dict[str, Any] = {k: [None] * n for k in (*TEMPS, *RUNS)}
    counts = [0] * n
    rows = conn.execute(_SQL, {"t0": t0, "step": step, "since": iso(t0), "until": iso(end)})
    for r in rows:
        b = r["b"]
        if b is None or not 0 <= b < n:
            continue
        counts[b] = r["n"]
        for k in TEMPS:
            out[k][b] = None if r[k] is None else round(r[k], 1)
        for k in RUNS:
            out[k][b] = round(r[k], 2)
    return {
        "range": rng,
        "step_s": step,
        "start": iso(t0),
        "end": iso(end),
        "units": units,
        "count": sum(counts),
        "t": [t0 + i * step for i in range(n)],
        **out,
        "n": counts,
    }


_ROOM_SQL = """
WITH raw AS (
  SELECT ts, snapshot_json,
    CASE WHEN :room = 'thermostat'
      THEN json_extract(snapshot_json, '$.tstat')
      ELSE (SELECT value FROM json_each(snapshot_json, '$.rooms') WHERE key = :room)
    END AS reading,
    instr(',' || COALESCE(stale_rooms, '') || ',', ',' || :room || ',') > 0 AS stale
  FROM samples WHERE ts >= :since AND ts < :until
), signals AS (
  SELECT (CAST(strftime('%s', ts) AS INTEGER) - :t0) / :step AS b,
    CASE WHEN json_type(reading, '$.temp') IN ('integer', 'real')
      THEN json_extract(reading, '$.temp') END AS temp,
    CASE WHEN json_type(reading, '$.age_min') IN ('integer', 'real')
      THEN json_extract(reading, '$.age_min') END AS age,
    json_extract(snapshot_json, '$.tstat.temp') AS indoor,
    COALESCE(json_extract(snapshot_json, '$.t_out'),
             json_extract(snapshot_json, '$.t_out_fc')) AS outdoor,
    json_extract(snapshot_json, '$.tstat.hvac_action') AS action,
    CASE json_extract(snapshot_json, '$.block')
      WHEN 'home' THEN :home_lo WHEN 'sleep' THEN :sleep_lo WHEN 'away' THEN :away_lo
    END AS lo,
    CASE json_extract(snapshot_json, '$.block')
      WHEN 'home' THEN :home_hi WHEN 'sleep' THEN :sleep_hi WHEN 'away' THEN :away_hi
    END AS hi,
    stale
  FROM raw
), measured AS (
  SELECT *, COALESCE(temp BETWEEN -1e308 AND 1e308 AND age BETWEEN 0 AND :stale_min
    AND NOT stale, 0) AS fresh
  FROM signals
)
SELECT b, COUNT(*) AS samples,
  SUM(CASE WHEN fresh THEN 1 ELSE 0 END) AS measured,
  SUM(CASE WHEN temp IS NOT NULL AND NOT fresh THEN 1 ELSE 0 END) AS stale,
  SUM(CASE WHEN temp IS NULL THEN 1 ELSE 0 END) AS missing,
  SUM(CASE WHEN fresh THEN temp END) AS temp_sum,
  MIN(CASE WHEN fresh THEN temp END) AS low,
  MAX(CASE WHEN fresh THEN temp END) AS high,
  AVG(CASE WHEN fresh THEN indoor END) AS indoor,
  AVG(CASE WHEN fresh THEN outdoor END) AS outdoor,
  SUM(CASE WHEN fresh AND typeof(indoor) IN ('integer', 'real')
    THEN temp - indoor END) AS offset_sum,
  SUM(CASE WHEN fresh AND typeof(indoor) IN ('integer', 'real')
    THEN 1 ELSE 0 END) AS paired,
  SUM(CASE WHEN fresh AND lo IS NOT NULL AND hi IS NOT NULL
    THEN 1 ELSE 0 END) AS assessed,
  SUM(CASE WHEN fresh AND temp BETWEEN lo AND hi THEN 1 ELSE 0 END) AS in_band,
  SUM(CASE WHEN fresh AND lo IS NOT NULL AND hi IS NOT NULL
    THEN MAX(0, lo - temp, temp - hi) END) AS excursion_sum,
  SUM(CASE WHEN fresh AND action = 'heating' THEN 1 ELSE 0 END) AS heat_min,
  SUM(CASE WHEN fresh AND action = 'cooling' THEN 1 ELSE 0 END) AS cool_min,
  SUM(CASE WHEN fresh AND action IN ('heating', 'cooling', 'idle', 'off', 'fan')
    THEN 1 ELSE 0 END) AS runtime_known
FROM measured GROUP BY b ORDER BY b
"""


def room_report(
    conn: sqlite3.Connection,
    now: datetime,
    rng: str,
    room: str,
    settings: Settings,
    season: Season,
) -> dict[str, Any]:
    """Bounded room-only aggregates. Each recorded minute has equal weight; missing and
    stale readings never become measurements. Bands are references using current settings,
    not a reconstruction of past weather, configuration or nudges."""
    span = RANGES[rng]
    step = 3600 if rng == "24h" else 21600 if rng == "7d" else 86400
    n = int(span.total_seconds()) // step
    end = int(now.astimezone(UTC).replace(second=0, microsecond=0).timestamp()) + 60
    t0 = end - n * step
    cfg = settings.rooms[room]
    params: dict[str, Any] = {
        "room": room,
        "stale_min": cfg.stale_min,
        "t0": t0,
        "step": step,
        "since": datetime.fromtimestamp(t0, UTC).isoformat(),
        "until": datetime.fromtimestamp(end, UTC).isoformat(),
    }
    blocks: tuple[BlockName, ...] = ("home", "sleep", "away")
    for block in blocks:
        lo, hi = room_band(block_band(settings, season, block), cfg.tol, season=season)
        params[f"{block}_lo"], params[f"{block}_hi"] = lo, hi
    buckets = {r["b"]: dict(r) for r in conn.execute(_ROOM_SQL, params)}
    rows: list[dict[str, Any]] = []
    for i in range(n):
        row = buckets.get(i, {"samples": 0, "measured": 0, "stale": 0, "missing": 0})
        row["start"] = datetime.fromtimestamp(t0 + i * step, UTC)
        row["end"] = datetime.fromtimestamp(t0 + (i + 1) * step, UTC)
        row["temp"] = row["temp_sum"] / row["measured"] if row["measured"] else None
        rows.append(row)

    def total(key: str) -> float:
        return sum(r.get(key) or 0 for r in rows)

    count, assessed, paired = total("measured"), total("assessed"), total("paired")
    latest, previous = rows[-1], rows[-2]
    change = (
        latest["temp"] - previous["temp"]
        if latest["measured"] >= 5 and previous["measured"] >= 5
        else None
    )
    return {
        "range": rng,
        "interval_h": step // 3600,
        "rows": rows,
        "measured": int(count),
        "samples": int(total("samples")),
        "stale": int(total("stale")),
        "missing": int(total("missing")),
        "coverage_pct": 100 * count / (span.total_seconds() / 60),
        "mean": total("temp_sum") / count if count else None,
        "low": min(r["low"] for r in rows if r["measured"]) if count else None,
        "high": max(r["high"] for r in rows if r["measured"]) if count else None,
        "offset": total("offset_sum") / paired if paired else None,
        "change": change,
        "assessed": int(assessed),
        "comfort_pct": 100 * total("in_band") / assessed if assessed else None,
        "outside_min": int(assessed - total("in_band")) if assessed else None,
        "excursion": total("excursion_sum") / assessed if assessed else None,
        "heat_min": int(total("heat_min")) if total("runtime_known") else None,
        "cool_min": int(total("cool_min")) if total("runtime_known") else None,
    }
