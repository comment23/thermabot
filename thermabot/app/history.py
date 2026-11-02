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
