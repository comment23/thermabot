"""Read-only room evidence and simulations. No actuator decisions or I/O."""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from thermabot.config import InsightsCfg, Settings
from thermabot.core.efficiency import EffDay, points
from thermabot.core.limits import COOL_SP_MAX, COOL_SP_MIN, HEAT_SP_MIN
from thermabot.core.planner import Choice, ctrl_step
from thermabot.core.schedule import active_zone
from thermabot.core.thermal import ThermalParams


@dataclass
class Meter:
    value: float
    source: str


@dataclass
class Observation:
    t: datetime
    temp: float | None = None
    indoor: float | None = None
    quality: Literal["fresh", "stale", "missing"] = "missing"
    humidity: float | None = None
    energy: Meter | None = None
    power: Meter | None = None
    action: str | None = None
    aux: bool | None = None
    cs: float | None = None
    house: str | None = None
    block: str | None = None


def rate_at(t: datetime, cfg: InsightsCfg) -> float | None:
    hour = t.astimezone(active_zone()).hour
    return cfg.peak_rate if cfg.peak_start_hour <= hour < cfg.peak_end_hour else cfg.off_peak_rate


def sensor_quality(rows: Sequence[Observation], expected_minutes: float) -> dict[str, Any]:
    measured = [r for r in rows if r.temp is not None]
    differences = [
        r.temp - r.indoor for r in measured if r.indoor is not None and r.temp is not None
    ]
    offset = statistics.mean(differences) if differences else None
    persistent = (
        len(differences) >= 120
        and offset is not None
        and abs(offset) >= 2
        and sum(abs(d) >= 2 and d * offset > 0 for d in differences) / len(differences) >= 0.8
    )
    return {
        "measured": len(measured),
        "stale": sum(r.quality == "stale" for r in rows),
        "missing": sum(r.quality == "missing" for r in rows),
        "coverage_pct": 100 * len(measured) / expected_minutes,
        "offset_f": offset,
        "persistent_disagreement": persistent,
        "paired_minutes": len(differences),
        "note": "Persistent disagreement can reflect real room conditions, "
        "not proven calibration drift.",
    }


def humidity_comfort(rows: Sequence[Observation], cfg: InsightsCfg) -> dict[str, Any]:
    paired = [r for r in rows if r.temp is not None and r.humidity is not None]
    values = [r.humidity for r in paired if r.humidity is not None]
    comfortable = sum(
        r.temp is not None
        and r.humidity is not None
        and abs(r.temp - cfg.ideal_temp_f) <= cfg.tolerance_f
        and 30 <= r.humidity <= 60
        for r in paired
    )
    return {
        "minutes": len(paired),
        "mean_rh": statistics.mean(values) if values else None,
        "outside_rh_minutes": sum(v < 30 or v > 60 for v in values),
        "comfortable_pct": 100 * comfortable / len(paired) if paired else None,
        "note": "Joint indicator: ideal temperature band and 30-60% RH; "
        "not a medical or mold diagnosis.",
    }


def energy_usage(rows: Sequence[Observation], cfg: InsightsCfg) -> dict[str, Any]:
    cumulative = any(r.energy is not None for r in rows)
    used = minutes = cost = 0.0
    pairs = resets = gaps = 0
    for a, b in zip(rows, rows[1:], strict=False):
        duration = (b.t - a.t).total_seconds() / 60
        x, y = (a.energy, b.energy) if cumulative else (a.power, b.power)
        if x is None or y is None:
            continue
        if duration <= 0 or duration > 2 or x.source != y.source:
            gaps += 1
            continue
        delta = y.value - x.value if cumulative else (x.value + y.value) / 2 * duration / 60
        if delta < 0:
            resets += 1
            continue
        used += delta
        minutes += duration
        pairs += 1
        rate = rate_at(a.t + (b.t - a.t) / 2, cfg)
        if rate is not None:
            cost += delta * rate
    return {
        "kwh": used if pairs else None,
        "cost": cost if pairs and cfg.peak_rate is not None else None,
        "currency": cfg.currency,
        "covered_minutes": minutes,
        "resets": resets,
        "gaps": gaps,
        "method": "cumulative meter deltas" if cumulative else "integrated measured power",
        "note": "Measured covered intervals only; gaps, resets and source changes excluded. "
        "Cost is an estimate using configured local-hour tariffs, excluding fixed charges.",
    }


def heat_pump_diagnostics(rows: Sequence[Observation]) -> dict[str, Any]:
    known = {"heating", "cooling", "idle", "off", "fan"}
    starts = short_runs = 0
    started: datetime | None = None
    previous: Observation | None = None
    for r in rows:
        if (
            previous is None
            or (r.t - previous.t) > timedelta(minutes=2)
            or r.action not in known
            or previous.action not in known
        ):
            started = None
        elif r.action != previous.action:
            if started is not None and previous.action in ("heating", "cooling"):
                short_runs += (r.t - started).total_seconds() < 180
            started = r.t if r.action in ("heating", "cooling") else None
            starts += started is not None
        previous = r
    aux = [r for r in rows if r.aux is not None]
    speeds = [r.cs for r in rows if r.cs is not None and r.action == "cooling"]
    return {
        "known_minutes": sum(r.action in known for r in rows),
        "starts": starts,
        "short_runs": short_runs,
        "cycling_alert": short_runs >= 3,
        "aux_minutes": sum(r.aux is True for r in aux) if aux else None,
        "aux_known_minutes": len(aux),
        "mean_cooling_speed": statistics.mean(speeds) if speeds else None,
        "note": "Central equipment observations. Runs across gaps or range boundaries "
        "are not inferred; "
        "three completed runs under 3 minutes trigger a review, not an equipment diagnosis.",
    }


def schedule_suggestions(rows: Sequence[Observation]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, int], list[Observation]] = defaultdict(list)
    for r in rows:
        local = r.t.astimezone(active_zone())
        if r.house in ("OCCUPIED", "EMPTY") and r.block == "home" and local.hour < 23:
            groups[("weekend" if local.weekday() >= 5 else "weekday", local.hour)].append(r)
    out = []
    for (kind, hour), samples in sorted(groups.items()):
        days: dict[str, list[Observation]] = defaultdict(list)
        for r in samples:
            days[r.t.astimezone(active_zone()).date().isoformat()].append(r)
        complete = [rs for rs in days.values() if len(rs) >= 48]
        if len(complete) < 5:
            continue
        rates = [sum(r.house == "EMPTY" for r in rs) / len(rs) for rs in complete]
        if sum(rate >= 0.8 for rate in rates) >= 5 and statistics.mean(rates) >= 0.8:
            out.append(
                {
                    "id": f"{kind}-{hour}",
                    "kind": kind,
                    "hour": hour,
                    "days": len(complete),
                    "away_pct": 100 * statistics.mean(rates),
                }
            )
    return out


def weather_comparison(before: Sequence[EffDay], after: Sequence[EffDay]) -> dict[str, Any]:
    for season in ("heat", "cool"):
        baseline, recent = points(before, season), points(after, season)
        if len(baseline) < 5 or len(recent) < 5:
            continue
        center = statistics.median(p.x for p in baseline)
        baseline = [p for p in baseline if abs(p.x - center) <= 5]
        recent = [p for p in recent if abs(p.x - center) <= 5]
        if len(baseline) < 5 or len(recent) < 5:
            continue
        base = statistics.median(p.y for p in baseline)
        latest = statistics.median(p.y for p in recent)
        if base <= 0:
            continue
        change = round(100 * (latest / base - 1), 6)
        return {
            "available": True,
            "season": season,
            "change_pct": change,
            "baseline": base,
            "recent": latest,
            "baseline_days": len(baseline),
            "recent_days": len(recent),
            "worse": latest >= base * 1.2,
            "note": "Central runtime per degree-day, compared within 5F of "
            "baseline median weather. "
            "Schedules, occupancy, maintenance and other changes may confound this proxy.",
        }
    return {
        "available": False,
        "note": "Need at least 5 qualifying, weather-comparable days in each "
        "window (>2 degree-days, >=80% coverage); no savings claim is made.",
    }


def simulate(
    times: Sequence[datetime],
    outdoor: Sequence[float],
    indoor: float,
    offset: float,
    params: ThermalParams,
    settings: Settings,
    mode: Literal["heat", "cool"],
    scenario: Literal["baseline", "setback", "tariff"],
    setback_f: float = 2,
    hours: int = 4,
) -> dict[str, Any]:
    cfg = settings.insights
    if not times or len(times) != len(outdoor):
        raise ValueError("simulation requires a nonempty, aligned forecast")
    if scenario == "tariff" and cfg.peak_rate is None:
        raise ValueError("tariff simulation requires configured prices")
    temp = indoor
    runtime = cost = 0.0
    comfortable = 0
    temperatures: list[float] = []
    safe = True
    for i, (t, to) in enumerate(zip(times, outdoor, strict=True)):
        center = cfg.ideal_temp_f - offset
        direction = 1 if mode == "heat" else -1
        if scenario == "setback" and i < hours * 4:
            center -= direction * setback_f
        if scenario == "tariff":
            peak = rate_at(t, cfg) == cfg.peak_rate
            next_peak = rate_at(t + timedelta(hours=1), cfg) == cfg.peak_rate
            center += direction * (
                -cfg.tolerance_f if peak else cfg.tolerance_f if next_peak else 0
            )
        # These are simulation setpoints only; no choice leaves this reporting module.
        choice = Choice(
            mode, center if mode == "heat" else None, center if mode == "cool" else None
        )
        local = t.astimezone(active_zone())
        temp, duty = ctrl_step(temp, to, local.hour + local.minute / 60, choice, params)
        runtime += abs(duty) * 15
        rate = rate_at(t, cfg)
        if rate is not None and cfg.rated_power_kw is not None:
            cost += abs(duty) * 0.25 * cfg.rated_power_kw * rate
        room_temp = temp + offset
        temperatures.append(room_temp)
        comfortable += abs(room_temp - cfg.ideal_temp_f) <= cfg.tolerance_f
        safe = safe and (
            settings.safety.floor_room_f <= room_temp <= settings.safety.ceil_room_f
            and settings.safety.floor_tstat_f <= temp <= settings.safety.ceil_tstat_f
            and center >= (HEAT_SP_MIN if mode == "heat" else COOL_SP_MIN)
            and center <= COOL_SP_MAX
        )
    return {
        "runtime_min": runtime,
        "comfort_pct": 100 * comfortable / len(times),
        "cost": cost if cfg.peak_rate is not None and cfg.rated_power_kw is not None else None,
        "low_f": min(temperatures),
        "high_f": max(temperatures),
        "safe": safe,
    }
