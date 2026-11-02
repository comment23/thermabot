"""v0.5.0 efficiency curve: the I/O side (core/efficiency.py holds the math).

`efficiency_daily` (migration 004) gets one row per completed local day, computed from the
1-minute `samples` table: hourly-integrated HDD/CDD (base 65 °F), heat/cool runtime and the
sample coverage. The 00:05 rollup writes yesterday; a one-time start-up backfill fills older
days in small chunks (one day per chunk, then a yield), never touching `samples`. Read-only
for everything else: no HA calls, no bot state, no planner/controller inputs.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from thermabot.app.jobs import (
    SAMPLE_RETENTION,
    _g,
    elapsed_minutes,
    load_samples,
    local_day_bounds,
    num,
)
from thermabot.core.efficiency import EffDay, ShadowIn, hourly_degree_days, summarize
from thermabot.core.metrics import MinuteSample
from thermabot.core.metrics import runtime as day_runtime
from thermabot.core.schedule import TZ
from thermabot.core.shadow import MinuteRow

log = logging.getLogger(__name__)

COLS = (
    "mean_out_f",
    "hdd",
    "cdd",
    "heat_min",
    "cool_min",
    "n_samples",
    "out_hours",
    "coverage_pct",
    "base_f",
)
PAUSE_S = 0.05  # yield to the loop between backfill chunks (one local day each)
METHOD = (
    "Degree-days use base 65 °F and hourly mean outdoor temperatures from the 1-minute"
    " samples. A day qualifies with more than 2 HDD (heat) or CDD (cool) and at least 80% of"
    " its minutes sampled. The curve is a robust (Huber) fit of runtime minutes per degree-day"
    " against the day's mean outdoor temperature, with a 95% band for the fitted line. Index ="
    " 7-day rolling median vs the first 7 qualifying days (100 = same; lower = less runtime"
    " per degree-day). See docs/efficiency.md."
)


def light_rows(rows: Sequence[tuple[datetime, dict[str, Any]]]) -> list[MinuteRow]:
    """Decode only what an efficiency row needs (outdoor temp + runtime signals)."""
    out: list[MinuteRow] = []
    for t, d in rows:
        if not isinstance(d, dict):
            continue
        out.append(
            MinuteRow(
                t=t,
                t_out=num(d.get("t_out") if d.get("t_out") is not None else d.get("t_out_fc")),
                hvac_action=_g(d, "tstat", "hvac_action"),
                blower=(num(d.get("blower")) or 0.0) > 0.5,
                cs=num(d.get("cs")),
            )
        )
    return out


def day_row(rows: Sequence[MinuteRow], day_minutes: int, base_f: float = 65.0) -> dict[str, Any]:
    """One efficiency_daily row from a local day's 1-minute rows. Missing data stays NULL."""
    hours: dict[datetime, list[float]] = {}
    for r in rows:
        if r.t_out is not None:
            # UTC keys distinguish the two occurrences of a repeated local hour.
            h = r.t.astimezone(TZ).replace(minute=0, second=0, microsecond=0).astimezone(UTC)
            hours.setdefault(h, []).append(r.t_out)
    dd = hourly_degree_days([sum(v) / len(v) for _, v in sorted(hours.items())], base_f)
    rt = (
        day_runtime(
            [MinuteSample(hvac_action=r.hvac_action, blower_active=r.blower, cs=r.cs) for r in rows]
        )
        if rows
        else None
    )
    return {
        "mean_out_f": None if dd is None else round(dd[0], 3),
        "hdd": None if dd is None else round(dd[1], 3),
        "cdd": None if dd is None else round(dd[2], 3),
        "heat_min": None if rt is None else rt.heat_min,
        "cool_min": None if rt is None else rt.cool_min,
        "n_samples": len(rows),
        "out_hours": len(hours),
        "coverage_pct": round(100.0 * len(rows) / day_minutes, 1) if day_minutes else 0.0,
        "base_f": base_f,
    }


def save_day(
    conn: sqlite3.Connection, day: str, row: dict[str, Any], now: datetime, source: str
) -> None:
    vals: dict[str, Any] = {"day": day, **{k: row[k] for k in COLS}, "source": source}
    vals["created_at"] = now.astimezone(UTC).isoformat()
    cols = ", ".join(vals)
    conn.execute(
        f"INSERT OR REPLACE INTO efficiency_daily ({cols}) VALUES ({', '.join('?' * len(vals))})",
        tuple(vals.values()),
    )


def compute_day(
    conn: sqlite3.Connection, d: date, base_f: float, now: datetime, source: str
) -> dict[str, Any] | None:
    """Compute and store one local day from `samples`; None (nothing written) if no rows."""
    start, end = local_day_bounds(d)
    rows = light_rows(load_samples(conn, start, end))
    if not rows:
        return None
    row = day_row(rows, elapsed_minutes(start, end), base_f)
    save_day(conn, d.isoformat(), row, now, source)
    return row


def missing_days(conn: sqlite3.Connection, now: datetime) -> list[date]:
    """Completed local days with samples but no efficiency_daily row, oldest first.
    Bounded by the sample retention window (120 d)."""
    r = conn.execute("SELECT MIN(ts) FROM samples").fetchone()
    if r is None or r[0] is None:
        return []
    first = datetime.fromisoformat(r[0]).astimezone(TZ).date()
    today = now.astimezone(TZ).date()
    have = {x[0] for x in conn.execute("SELECT day FROM efficiency_daily")}
    out: list[date] = []
    d = max(first, today - SAMPLE_RETENTION)
    while d < today:
        if d.isoformat() not in have:
            out.append(d)
        d += timedelta(days=1)
    return out


async def backfill(
    conn: sqlite3.Connection,
    now: datetime,
    base_f: float = 65.0,
    pause_s: float = PAUSE_S,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> dict[str, Any]:
    """One-time start-up backfill. Idempotent (only days without a row) and chunked: one
    local day per chunk (<= ~1500 rows), then a yield so the tick/heartbeat keep running.
    Days with no samples get no row. Never writes `samples`."""
    t0 = time.monotonic()
    days = missing_days(conn, now)
    written = 0
    max_chunk = 0.0
    for d in days:
        c0 = time.monotonic()
        if compute_day(conn, d, base_f, now, "backfill") is not None:
            written += 1
        max_chunk = max(max_chunk, time.monotonic() - c0)
        await sleep(pause_s)
    q = qualifying(conn)
    res = {
        "candidates": len(days),
        "written": written,
        "qualifying_heat": q[0],
        "qualifying_cool": q[1],
        "seconds": round(time.monotonic() - t0, 3),
        "max_chunk_s": round(max_chunk, 3),
    }
    log.info(
        "efficiency backfill done: %d days written of %d candidates; qualifying heat %d,"
        " cool %d; %.2f s (max chunk %.3f s)",
        written,
        len(days),
        q[0],
        q[1],
        res["seconds"],
        res["max_chunk_s"],
    )
    return res


def qualifying(conn: sqlite3.Connection) -> tuple[int, int]:
    """(heat, cool) qualifying day counts, same rules as core.efficiency.points."""
    s = summarize(_eff_days(conn, "0000-00-00"), [])
    return s.heat.n, s.cool.n


def _eff_days(conn: sqlite3.Connection, since: str) -> list[EffDay]:
    return [
        EffDay(
            day=r["day"],
            mean_out_f=r["mean_out_f"],
            hdd=r["hdd"],
            cdd=r["cdd"],
            heat_min=r["heat_min"],
            cool_min=r["cool_min"],
            coverage_pct=r["coverage_pct"] or 0.0,
        )
        for r in conn.execute(
            "SELECT * FROM efficiency_daily WHERE day >= ? ORDER BY day", (since,)
        )
    ]


def _shadow(conn: sqlite3.Connection, since: str) -> list[ShadowIn]:
    out: list[ShadowIn] = []
    for r in conn.execute(
        "SELECT day, actual_heat_min, actual_cool_min, saved_min FROM shadow_daily"
        " WHERE day >= ? ORDER BY day",
        (since,),
    ):
        h, c = r["actual_heat_min"], r["actual_cool_min"]
        out.append(
            ShadowIn(
                day=r["day"],
                actual_min=None if h is None and c is None else (h or 0) + (c or 0),
                saved_min=r["saved_min"],
            )
        )
    return out


def report(conn: sqlite3.Connection, now: datetime, days: int = 365) -> dict[str, Any]:
    """Everything GET /api/efficiency and the Efficiency page show. Read-only."""
    since = (now.astimezone(TZ) - timedelta(days=days)).date().isoformat()
    out = summarize(_eff_days(conn, since), _shadow(conn, since)).model_dump(mode="json")
    out["method"] = METHOD
    return out
